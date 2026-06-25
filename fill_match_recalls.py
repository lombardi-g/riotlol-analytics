import requests
import os
import time
import psycopg
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ["API_KEY"]
database_url = os.environ["DATABASE_URL"]
# my puuid
puuid = "axPj0EmED_6dbM3axHyct2teNkP1pEa92W3UJYecaCYhL0BdXyONbQV4mzpT5YlkhaQoxmw96Ro8ug"

# lord semi puuid
# puuid = "fLJxhjWn6UTyK2ACGhBogViw-gnFs-iSdRQTzOqxjlSDdRMilzUQOPmkxEaIVCvFzMzrM-NaFQqZMg"

# --- tuning knobs ----------------------------------------------------------
# Purchases farther apart than this start a new "base visit" cluster.
CLUSTER_GAP_MS = 8_000
# Clusters before this are the opening fountain buy, not a recall.
GAME_START_CUTOFF_MS = 90_000
# A cluster within this window after a death is respawn shopping, not a recall.
DEATH_WINDOW_MS = 12_000


def check_match_id():
    """match_ids in `matches` that haven't been processed into `match_recalls` yet."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT match_id FROM matches")
            all_ids = [row[0] for row in cur.fetchall()]

            # one match -> many recall rows, so "done" = matchId already present
            cur.execute('SELECT DISTINCT "matchId" FROM match_recalls')
            done_ids = {row[0] for row in cur.fetchall()}

    return [m for m in all_ids if m not in done_ids]


def fetch_timeline(match_id):
    """Pull the match timeline from Riot, backing off on rate limits and
    retrying transient network drops (e.g. ChunkedEncodingError)."""
    api_url = (
        f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        f"?api_key={api_key}"
    )
    attempts = 0
    while True:
        try:
            response = requests.get(api_url, timeout=30)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 10))
                print(f"Rate limited, sleeping {retry_after}s...")
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            attempts += 1
            if attempts >= 5:
                raise
            wait = min(2 ** attempts, 30)  # 2s, 4s, 8s, 16s
            print(f"  network error ({type(e).__name__}), retry {attempts}/4 in {wait}s...")
            time.sleep(wait)


def get_champion_and_team(match_id):
    """Champion name + team id for our puuid, read from the already-filled match_data."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "championName", "myTeam" FROM match_data '
                'WHERE "matchId" = %s AND puuid = %s',
                (match_id, puuid),
            )
            row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def participant_id_for_puuid(info):
    """Timeline maps puuid -> participantId in info.participants."""
    for p in info["participants"]:
        if p["puuid"] == puuid:
            return p["participantId"]
    return None


def collect_events(frames, pid):
    """Net purchases (after undos) and death timestamps for our participant."""
    purchases = []   # list of (timestamp_ms, item_id), in event order
    deaths = []      # list of timestamp_ms

    for frame in frames:
        for ev in frame["events"]:
            etype = ev["type"]
            if etype == "ITEM_PURCHASED" and ev.get("participantId") == pid:
                purchases.append((ev["timestamp"], ev["itemId"]))
            elif etype == "ITEM_UNDO" and ev.get("participantId") == pid:
                # Undo removes the most recent purchase of beforeId.
                before_id = ev.get("beforeId")
                for idx in range(len(purchases) - 1, -1, -1):
                    if purchases[idx][1] == before_id:
                        purchases.pop(idx)
                        break
            elif etype == "CHAMPION_KILL" and ev.get("victimId") == pid:
                deaths.append(ev["timestamp"])

    purchases.sort(key=lambda x: x[0])
    deaths.sort()
    return purchases, deaths


def cluster_purchases(purchases):
    """Group purchases that happen close together into one base visit each."""
    clusters = []  # list of (start_ms, [item_ids])
    current = []
    for ts, item_id in purchases:
        if current and ts - current[-1][0] > CLUSTER_GAP_MS:
            clusters.append((current[0][0], [i for _, i in current]))
            current = []
        current.append((ts, item_id))
    if current:
        clusters.append((current[0][0], [i for _, i in current]))
    return clusters


def gold_at(frames, pid, ts):
    """currentGold from the per-minute frame nearest to ts (+ that frame's time)."""
    nearest = min(frames, key=lambda fr: abs(fr["timestamp"] - ts))
    pf = nearest["participantFrames"][str(pid)]
    return pf["currentGold"], nearest["timestamp"]


def classify(start_ms, deaths):
    if start_ms < GAME_START_CUTOFF_MS:
        return "game_start"
    if any(0 <= start_ms - d <= DEATH_WINDOW_MS for d in deaths):
        return "post_death"
    return "recall"


def build_rows(match_id, timeline):
    """One row per base visit for our puuid, or [] if the player isn't in the match."""
    info = timeline["info"]
    frames = info["frames"]

    pid = participant_id_for_puuid(info)
    if pid is None:
        return []

    champion, team_id = get_champion_and_team(match_id)
    if team_id is None:  # fallback if match_data isn't filled for this game
        team_id = 100 if pid <= 5 else 200

    purchases, deaths = collect_events(frames, pid)
    clusters = cluster_purchases(purchases)

    rows = []
    recall_index = 0
    for start_ms, item_ids in clusters:
        visit_type = classify(start_ms, deaths)
        if visit_type == "recall":
            recall_index += 1
            idx = recall_index
        else:
            idx = None

        gold, frame_ms = gold_at(frames, pid, start_ms)

        rows.append({
            "matchId": match_id,
            "puuid": puuid,
            "champion": champion,
            "team_id": team_id,
            "recall_index": idx,
            "visit_type": visit_type,
            "timestamp_ms": start_ms,
            "game_time_s": round(start_ms / 1000, 1),
            "gold_snapshot": gold,
            "gold_snapshot_frame_ms": frame_ms,
            "items_purchased": item_ids,   # psycopg sends a list as int4[]
            "item_count": len(item_ids),
        })
    return rows


INSERT_COLUMNS = [
    "matchId", "puuid", "champion", "team_id", "recall_index", "visit_type",
    "timestamp_ms", "game_time_s", "gold_snapshot", "gold_snapshot_frame_ms",
    "items_purchased", "item_count",
]
_quoted_cols = ", ".join('"' + c + '"' for c in INSERT_COLUMNS)
_placeholders = ", ".join(["%s"] * len(INSERT_COLUMNS))
INSERT_SQL = (
    f"INSERT INTO match_recalls ({_quoted_cols}) "
    f"VALUES ({_placeholders}) "
    f'ON CONFLICT ("matchId", puuid, timestamp_ms) DO NOTHING'
)


def insert_timeline_data(match_id):
    """Fetch one match's timeline, derive recalls, insert all rows in one transaction."""
    timeline = fetch_timeline(match_id)
    rows = build_rows(match_id, timeline)
    if not rows:
        print(f"{match_id}: puuid not found, skipping")
        return

    values = [tuple(r[c] for c in INSERT_COLUMNS) for r in rows]
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, values)
        conn.commit()

    recalls = sum(1 for r in rows if r["visit_type"] == "recall")
    print(f"{match_id}: {len(rows)} base visits ({recalls} recalls) inserted")


if __name__ == "__main__":
    matches_to_insert = check_match_id()
    print(f"{len(matches_to_insert)} matches to insert.")
    for i, each_match_id in enumerate(matches_to_insert, 1):
        print(f"[{i}/{len(matches_to_insert)}]", end=" ")
        insert_timeline_data(each_match_id)
        time.sleep(1.2)  # stay under the dev key rate limit

import requests
import os
import time
import psycopg
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ["API_KEY"]
database_url = os.environ["DATABASE_URL"]

# my puuid
# puuid = "axPj0EmED_6dbM3axHyct2teNkP1pEa92W3UJYecaCYhL0BdXyONbQV4mzpT5YlkhaQoxmw96Ro8ug"

# lord semi puuid
puuid = "fLJxhjWn6UTyK2ACGhBogViw-gnFs-iSdRQTzOqxjlSDdRMilzUQOPmkxEaIVCvFzMzrM-NaFQqZMg"

# --- map geometry ----------------------------------------------------------
# Summoner's Rift is ~14820 x 14820. Blue base is the (0,0) corner, red base
# the (max,max) corner. We only need a rough lane region for the kill location;
# the victim's role (target_lane) is the more reliable "which lane" signal.
MID_BAND = 1800   # kills within this distance of the main diagonal are "MID"

# A laner victim (anything but JUNGLE) is what makes a kill count as a gank.
LANE_ROLES = {"TOP", "MIDDLE", "BOTTOM", "UTILITY"}


def region_of(x, y):
    """Crude lane region from a kill position: MID band around the main
    diagonal, otherwise the BOT wedge (x > y) or the TOP wedge."""
    if x is None or y is None:
        return None
    if abs(x - y) < MID_BAND:
        return "MID"
    return "BOT" if x > y else "TOP"


def check_match_id():
    """match_ids in `matches` that haven't been processed into `match_ganks` yet."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT match_id FROM matches")
            all_ids = [row[0] for row in cur.fetchall()]

            # one match -> many gank rows (or zero), so "done" = matchId present
            cur.execute('SELECT DISTINCT "matchId" FROM match_ganks')
            done_ids = {row[0] for row in cur.fetchall()}

    return [m for m in all_ids if m not in done_ids]


def ensure_table():
    """Create match_ganks if it doesn't exist yet."""
    ddl = """
    CREATE TABLE IF NOT EXISTS match_ganks (
        id                  SERIAL PRIMARY KEY,
        "matchId"           TEXT    NOT NULL,
        puuid               TEXT    NOT NULL,
        champion            TEXT,
        team_id             INT,
        participation_index INT,        -- 1-based over ALL kill participations
        gank_index          INT,        -- 1-based over rows where is_gank is true
        is_gank             BOOLEAN,
        kill_type           TEXT,       -- 'kill' or 'assist'
        timestamp_ms        BIGINT,
        game_time_s         REAL,
        target_puuid        TEXT,
        target_champion     TEXT,
        target_lane         TEXT,       -- victim role: TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY
        region              TEXT,       -- lane region of the kill position
        position_x          INT,
        position_y          INT,
        assist_count        INT,
        jungle_cs_before    INT,        -- jungleMinionsKilled at frame <= gank (monster count, sampled per-minute)
        total_cs_before     INT,        -- minionsKilled (lane CS) at that frame
        level               INT,
        gold_snapshot       INT,
        frame_ms            BIGINT,     -- timestamp of the frame used for the snapshot
        UNIQUE ("matchId", puuid, timestamp_ms)
    );
    """
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def _fetch(url):
    """GET with 429 backoff and a few retries on transient network drops."""
    attempts = 0
    while True:
        try:
            response = requests.get(url, timeout=30)
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
            wait = min(2 ** attempts, 30)
            print(f"  network error ({type(e).__name__}), retry {attempts}/4 in {wait}s...")
            time.sleep(wait)


def fetch_match(match_id):
    """Post-game match JSON - we need it for the roster (role/champion/team per player)."""
    return _fetch(
        f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}"
        f"?api_key={api_key}"
    )


def fetch_timeline(match_id):
    """Timeline JSON - the per-minute frames and the CHAMPION_KILL event stream."""
    return _fetch(
        f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        f"?api_key={api_key}"
    )


def build_roster(match_json):
    """participantId -> {puuid, champion, team_id, lane}, built from the match endpoint
    (the timeline only knows puuid<->participantId, not roles or champions)."""
    roster = {}
    for p in match_json["info"]["participants"]:
        roster[p["participantId"]] = {
            "puuid": p["puuid"],
            "champion": p["championName"],
            "team_id": p["teamId"],
            "lane": p.get("teamPosition") or None,  # "" for some queues -> None
        }
    return roster


def participant_id_for_puuid(timeline_info):
    for p in timeline_info["participants"]:
        if p["puuid"] == puuid:
            return p["participantId"]
    return None


def frame_at_or_before(frames, ts):
    """The latest per-minute frame whose timestamp is <= ts ('what he had cleared
    just before the gank'). Frames include t=0, so this always finds one."""
    chosen = frames[0]
    for fr in frames:
        if fr["timestamp"] <= ts:
            chosen = fr
        else:
            break
    return chosen


def collect_gank_kills(frames, pid):
    """Every CHAMPION_KILL our player (pid) participated in - as killer or assister -
    in chronological order. Returns list of dicts with the event context."""
    out = []
    for frame in frames:
        for ev in frame["events"]:
            if ev["type"] != "CHAMPION_KILL":
                continue
            assisters = ev.get("assistingParticipantIds", []) or []
            is_killer = ev.get("killerId") == pid
            is_assist = pid in assisters
            if not (is_killer or is_assist):
                continue
            pos = ev.get("position") or {}
            out.append({
                "timestamp": ev["timestamp"],
                "victim_id": ev.get("victimId"),
                "kill_type": "kill" if is_killer else "assist",
                "assist_count": len(assisters),
                "x": pos.get("x"),
                "y": pos.get("y"),
            })
    out.sort(key=lambda e: e["timestamp"])
    return out


def build_rows(match_id, match_json, timeline):
    """One row per kill our tracked player participated in, with gank flag + jungle
    context. [] if the player isn't in the match."""
    info = timeline["info"]
    frames = info["frames"]

    pid = participant_id_for_puuid(info)
    if pid is None:
        return []

    roster = build_roster(match_json)
    me = roster.get(pid, {})

    kills = collect_gank_kills(frames, pid)

    rows = []
    participation_index = 0
    gank_index = 0
    for ev in kills:
        participation_index += 1
        victim = roster.get(ev["victim_id"], {})
        target_lane = victim.get("lane")

        is_gank = target_lane in LANE_ROLES
        if is_gank:
            gank_index += 1
            g_idx = gank_index
        else:
            g_idx = None

        fr = frame_at_or_before(frames, ev["timestamp"])
        pf = fr["participantFrames"][str(pid)]

        rows.append({
            "matchId": match_id,
            "puuid": puuid,
            "champion": me.get("champion"),
            "team_id": me.get("team_id"),
            "participation_index": participation_index,
            "gank_index": g_idx,
            "is_gank": is_gank,
            "kill_type": ev["kill_type"],
            "timestamp_ms": ev["timestamp"],
            "game_time_s": round(ev["timestamp"] / 1000, 1),
            "target_puuid": victim.get("puuid"),
            "target_champion": victim.get("champion"),
            "target_lane": target_lane,
            "region": region_of(ev["x"], ev["y"]),
            "position_x": ev["x"],
            "position_y": ev["y"],
            "assist_count": ev["assist_count"],
            "jungle_cs_before": pf.get("jungleMinionsKilled"),
            "total_cs_before": pf.get("minionsKilled"),
            "level": pf.get("level"),
            "gold_snapshot": pf.get("currentGold"),
            "frame_ms": fr["timestamp"],
        })
    return rows


INSERT_COLUMNS = [
    "matchId", "puuid", "champion", "team_id", "participation_index", "gank_index",
    "is_gank", "kill_type", "timestamp_ms", "game_time_s", "target_puuid",
    "target_champion", "target_lane", "region", "position_x", "position_y",
    "assist_count", "jungle_cs_before", "total_cs_before", "level",
    "gold_snapshot", "frame_ms",
]
_quoted_cols = ", ".join('"' + c + '"' for c in INSERT_COLUMNS)
_placeholders = ", ".join(["%s"] * len(INSERT_COLUMNS))
INSERT_SQL = (
    f"INSERT INTO match_ganks ({_quoted_cols}) "
    f"VALUES ({_placeholders}) "
    f'ON CONFLICT ("matchId", puuid, timestamp_ms) DO NOTHING'
)


def insert_match_ganks(match_id):
    """Fetch one match's roster + timeline, derive gank rows, insert in one transaction.
    Rows are still written when the player had zero ganks? No - we insert a marker row
    only when there are kills. To mark a fully-processed-but-gankless match, see note below."""
    match_json = fetch_match(match_id)
    time.sleep(1.2)  # two API calls per match - space them under the dev-key limit
    timeline = fetch_timeline(match_id)

    rows = build_rows(match_id, match_json, timeline)

    if not rows:
        # Player not in match, OR player had zero kill participations. Either way we
        # write a sentinel so check_match_id() treats this match as done and doesn't
        # refetch it every run.
        _write_sentinel(match_id, match_json, timeline)
        print(f"{match_id}: 0 gank participations (sentinel written)")
        return

    values = [tuple(r[c] for c in INSERT_COLUMNS) for r in rows]
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, values)
        conn.commit()

    ganks = sum(1 for r in rows if r["is_gank"])
    print(f"{match_id}: {len(rows)} kill participations ({ganks} ganks) inserted")


def _write_sentinel(match_id, match_json, timeline):
    """Insert a single non-gank row (timestamp_ms = -1) so a gankless/absent match is
    recorded as processed. is_gank is NULL on the sentinel so it's easy to exclude."""
    info = timeline["info"]
    pid = participant_id_for_puuid(info)
    me = build_roster(match_json).get(pid, {}) if pid else {}
    row = {c: None for c in INSERT_COLUMNS}
    row.update({
        "matchId": match_id,
        "puuid": puuid,
        "champion": me.get("champion"),
        "team_id": me.get("team_id"),
        "timestamp_ms": -1,
    })
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(INSERT_SQL, tuple(row[c] for c in INSERT_COLUMNS))
        conn.commit()


if __name__ == "__main__":
    ensure_table()
    matches_to_insert = check_match_id()
    print(f"{len(matches_to_insert)} matches to insert.")
    for i, each_match_id in enumerate(matches_to_insert, 1):
        print(f"[{i}/{len(matches_to_insert)}]", end=" ")
        insert_match_ganks(each_match_id)
        time.sleep(1.2)  # stay under the dev key rate limit

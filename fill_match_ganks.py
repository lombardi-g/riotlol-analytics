import requests
import os
import time
import psycopg
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ["API_KEY"]
db_url = os.environ["DATABASE_URL"]

# my puuid
puuid = "axPj0EmED_6dbM3axHyct2teNkP1pEa92W3UJYecaCYhL0BdXyONbQV4mzpT5YlkhaQoxmw96Ro8ug"

# lord semi puuid
# puuid = "fLJxhjWn6UTyK2ACGhBogViw-gnFs-iSdRQTzOqxjlSDdRMilzUQOPmkxEaIVCvFzMzrM-NaFQqZMg"


LANE_ROLES = {"TOP", "MIDDLE", "BOTTOM", "UTILITY"}
COLS = [
    "matchId", "puuid", "champion", "team_id", "participation_index", "gank_index",
    "is_gank", "kill_type", "timestamp_ms", "game_time_s", "target_puuid",
    "target_champion", "target_lane", "region", "position_x", "position_y",
    "assist_count", "jungle_cs_before", "total_cs_before", "level", "gold_snapshot", "frame_ms"
]
INS_SQL = f"""INSERT INTO match_ganks ({', '.join(f'"{c}"' for c in COLS)}) 
              VALUES ({', '.join(['%s']*len(COLS))}) 
              ON CONFLICT ("matchId", puuid, timestamp_ms) DO NOTHING"""


def fetch_riot(path):
    """GET requests with 429 backoff and exponential retries."""
    for attempt in range(5):
        try:
            res = requests.get(f"https://americas.api.riotgames.com/lol/match/v5/matches/{path}?api_key={api_key}", timeout=30)
            if res.status_code == 429:
                time.sleep(int(res.headers.get("Retry-After", 10)))
                continue
            res.raise_for_status()
            return res.json()
        except requests.RequestException:
            if attempt == 4: raise
            time.sleep(min(2 ** attempt, 30))


def get_pending_matches():
    """Returns match IDs that haven't been processed using set subtraction."""
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        all_ids = {r[0] for r in cur.execute("SELECT match_id FROM matches").fetchall()}
        done_ids = {r[0] for r in cur.execute('SELECT DISTINCT "matchId" FROM match_ganks').fetchall()}
        return list(all_ids - done_ids)


def process_match(match_id):
    """Fetches match + timeline, formats gank data, and inserts into DB."""
    match_json = fetch_riot(match_id)
    time.sleep(1.2)  # Space out API calls
    timeline = fetch_riot(f"{match_id}/timeline")

    info, frames = timeline["info"], timeline["info"]["frames"]
    pid = next((p["participantId"] for p in info["participants"] if p["puuid"] == puuid), None)
    roster = {p["participantId"]: p for p in match_json["info"]["participants"]}
    me = roster.get(pid, {})

    rows, p_idx, g_idx = [], 0, 0
    if pid:
        for fr in frames:
            for ev in fr["events"]:
                if ev["type"] != "CHAMPION_KILL": continue
                
                assists = ev.get("assistingParticipantIds", [])
                is_killer = ev.get("killerId") == pid
                if not (is_killer or pid in assists): continue
                
                p_idx += 1
                vic = roster.get(ev.get("victimId"), {})
                lane = vic.get("teamPosition")
                is_gank = lane in LANE_ROLES
                if is_gank: g_idx += 1
                
                ts, pos = ev["timestamp"], ev.get("position", {})
                x, y = pos.get("x"), pos.get("y")
                region = None if x is None else ("MID" if abs(x - y) < 1800 else "BOT" if x > y else "TOP")
                
                # Find the latest frame at or before the event timestamp
                snap_fr = next((f for f in reversed(frames) if f["timestamp"] <= ts), frames[0])
                pf = snap_fr["participantFrames"].get(str(pid), {})

                rows.append({
                    "matchId": match_id, "puuid": puuid, "champion": me.get("championName"),
                    "team_id": me.get("teamId"), "participation_index": p_idx,
                    "gank_index": g_idx if is_gank else None, "is_gank": is_gank,
                    "kill_type": "kill" if is_killer else "assist", "timestamp_ms": ts,
                    "game_time_s": round(ts / 1000, 1), "target_puuid": vic.get("puuid"),
                    "target_champion": vic.get("championName"), "target_lane": lane,
                    "region": region, "position_x": x, "position_y": y,
                    "assist_count": len(assists), "jungle_cs_before": pf.get("jungleMinionsKilled"),
                    "total_cs_before": pf.get("minionsKilled"), "level": pf.get("level"),
                    "gold_snapshot": pf.get("currentGold"), "frame_ms": snap_fr["timestamp"]
                })

    if not rows:
        # Write a sentinel row so we don't process this match again
        rows.append({c: None for c in COLS})
        rows[0].update({"matchId": match_id, "puuid": puuid, "champion": me.get("championName"), "team_id": me.get("teamId"), "timestamp_ms": -1})

    values = [tuple(r.get(c) for c in COLS) for r in rows]
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.executemany(INS_SQL, values)
        conn.commit()

    ganks = sum(1 for r in rows if r.get("is_gank"))
    print(f"{match_id}: {len(rows) if p_idx else 0} kill participations ({ganks} ganks) inserted")


if __name__ == "__main__":
    matches_to_insert = get_pending_matches()
    print(f"{len(matches_to_insert)} matches to insert.")
    
    for i, m_id in enumerate(matches_to_insert, 1):
        print(f"[{i}/{len(matches_to_insert)}]", end=" ")
        process_match(m_id)
        time.sleep(1.2)
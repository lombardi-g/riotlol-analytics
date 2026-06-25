import requests
import os
import time
from datetime import datetime, timezone
import psycopg
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ["API_KEY"]
database_url = os.environ["DATABASE_URL"]
# my puuid
puuid = "axPj0EmED_6dbM3axHyct2teNkP1pEa92W3UJYecaCYhL0BdXyONbQV4mzpT5YlkhaQoxmw96Ro8ug"

# lord semi puuid
# puuid = "fLJxhjWn6UTyK2ACGhBogViw-gnFs-iSdRQTzOqxjlSDdRMilzUQOPmkxEaIVCvFzMzrM-NaFQqZMg"

# --- constants -------------------------------------------------------------
AFTERSHOCK_PERK = 8439         
SPELL_FLASH = 4
SPELL_GHOST = 6
SPELL_SMITE = 11

# Maps the Riot "objectives" sub-keys to our team/enemy column suffixes.
# objectives -> {"dragon": {...}, "baron": {...}, "horde": {...}, "riftHerald": {...}}
OBJECTIVE_KEYS = {
    "Dragons": "dragon",
    "Barons": "baron",
    "Grubs": "horde",       # the void grubs are called "horde" in the JSON
    "Herald": "riftHerald",
}


def get_match_ids():
    """Return the match_ids that exist in `matches` but not yet in `match_data`."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT match_id FROM matches")
            all_ids = [row[0] for row in cur.fetchall()]

            cur.execute('SELECT "matchId" FROM match_data')
            done_ids = {row[0] for row in cur.fetchall()}

    to_insert = [m for m in all_ids if m not in done_ids]
    return to_insert


def get_data_columns():
    """Column name -> data_type for match_data, in definition order, minus the id PK."""
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'match_data' ORDER BY ordinal_position"
            )
            cols = [(name, dtype) for name, dtype in cur.fetchall() if name != "id"]
    return cols


def fetch_match(match_id):
    """Pull the full match JSON from Riot, backing off on rate limits."""
    api_url = (
        f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}"
        f"?api_key={api_key}"
    )
    while True:
        response = requests.get(api_url)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            print(f"Rate limited, sleeping {retry_after}s...")
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response.json()


def epoch_ms_to_dt(ms):
    """Riot timestamps are epoch milliseconds; store them as naive UTC datetimes."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def summoner_casts(participant, spell_id):
    """Total casts of `spell_id`, checking both summoner-spell slots."""
    total = 0
    if participant["summoner1Id"] == spell_id:
        total += participant["summoner1Casts"]
    if participant["summoner2Id"] == spell_id:
        total += participant["summoner2Casts"]
    return total


def aftershock_vars(participant):
    """Return (damageMitigated, damageDealt) if Aftershock was the keystone, else (None, None)."""
    for style in participant.get("perks", {}).get("styles", []):
        if style.get("description") != "primaryStyle":
            continue
        for selection in style.get("selections", []):
            if selection.get("perk") == AFTERSHOCK_PERK:
                # var1 = damage dealt, var2 = damage mitigated. Caller expects
                # (mitigated, dealt), so return var2 first.
                return selection.get("var2"), selection.get("var1")
    return None, None


def build_special_values(match_json, participant, my_team_obj, enemy_team_obj):
    """Computed/nested columns that don't map 1:1 to a participant key."""
    info = match_json["info"]
    mitigated, dealt = aftershock_vars(participant)

    special = {
        "matchId": match_json["metadata"]["matchId"],
        "puuid": participant["puuid"],
        "gameCreation": epoch_ms_to_dt(info.get("gameCreation")),
        "gameDuration": info.get("gameDuration"),
        "gameEnd": epoch_ms_to_dt(info.get("gameEndTimestamp")),
        "gameVersion": info.get("gameVersion"),
        "mapId": info.get("mapId"),
        "myTeam": participant["teamId"],
        # Remake / early-surrender flag (same value for every participant)
        "gameEndedInEarlySurrender": participant.get("gameEndedInEarlySurrender"),
        # Aftershock (only populated when the keystone matches)
        "afterShockDamageMitigated": mitigated,
        "afterShockDamageDealt": dealt,
        # Summoner spell casts
        "flashCasts": summoner_casts(participant, SPELL_FLASH),
        "ghostCasts": summoner_casts(participant, SPELL_GHOST),
        "smiteCasts": summoner_casts(participant, SPELL_SMITE),
        # First dragon belongs to my team?
        "firstDragon": bool(my_team_obj["dragon"]["first"]),
    }

    # Team / enemy objective counts
    for suffix, json_key in OBJECTIVE_KEYS.items():
        special[f"team{suffix}"] = my_team_obj[json_key]["kills"]
        special[f"enemy{suffix}"] = enemy_team_obj[json_key]["kills"]

    return special


def resolve_value(column, participant, challenges, special):
    """Find the value for `column`: special first, then participant, then challenges."""
    if column in special:
        return special[column]
    if column in participant:
        return participant[column]
    if column in challenges:
        return challenges[column]
    return None


def build_row(match_json, columns):
    """Build the ordered list of values for one match, or None if our puuid isn't in it."""
    info = match_json["info"]

    participant = next(
        (p for p in info["participants"] if p["puuid"] == puuid), None
    )
    if participant is None:
        return None

    challenges = participant.get("challenges", {})

    # Split the two teams into "mine" and "the enemy's".
    my_team_id = participant["teamId"]
    my_team_obj = next(t for t in info["teams"] if t["teamId"] == my_team_id)["objectives"]
    enemy_team_obj = next(t for t in info["teams"] if t["teamId"] != my_team_id)["objectives"]

    special = build_special_values(match_json, participant, my_team_obj, enemy_team_obj)

    row = []
    for name, dtype in columns:
        value = resolve_value(name, participant, challenges, special)
        # Some columns are stored as text but arrive as ints (basicPings,
        # totalTimeSpentDead, ...) - coerce so Postgres accepts them.
        if value is not None and dtype in ("text", "character varying") and not isinstance(value, str):
            value = str(value)
        row.append(value)
    return row


def insert_match_data(match_ids):
    if not match_ids:
        print("No matches to process.")
        return

    columns = get_data_columns()
    col_names = [name for name, _ in columns]
    quoted = ", ".join(f'"{name}"' for name in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    insert_sql = (
        f"INSERT INTO match_data ({quoted}) VALUES ({placeholders}) "
        f'ON CONFLICT ("matchId") DO NOTHING'
    )

    inserted = 0
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for i, match_id in enumerate(match_ids, 1):
                match_json = fetch_match(match_id)
                row = build_row(match_json, columns)
                if row is None:
                    print(f"[{i}/{len(match_ids)}] {match_id}: puuid not found, skipping")
                    continue

                cur.execute(insert_sql, row)
                inserted += 1
                print(f"[{i}/{len(match_ids)}] {match_id}: inserted")

                conn.commit()
                time.sleep(1.2)  # stay under the dev key rate limit

    print(f"Done. Inserted {inserted} matches.")


if __name__ == "__main__":
    matches_to_insert = get_match_ids()
    print(f"{len(matches_to_insert)} matches to insert.")
    insert_match_data(matches_to_insert)

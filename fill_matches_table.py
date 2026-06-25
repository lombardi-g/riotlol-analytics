import requests
import os
import time
from datetime import datetime, timezone
import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ["API_KEY"]
database_url = os.environ["DATABASE_URL"]
# my puuid
puuid = "axPj0EmED_6dbM3axHyct2teNkP1pEa92W3UJYecaCYhL0BdXyONbQV4mzpT5YlkhaQoxmw96Ro8ug"

# lord semi puuid
# puuid = "fLJxhjWn6UTyK2ACGhBogViw-gnFs-iSdRQTzOqxjlSDdRMilzUQOPmkxEaIVCvFzMzrM-NaFQqZMg"


def find_all_matches(year):
    matches_in_year = []
    # Riot wants epoch seconds, not a year number
    start_time = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end_time = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

    start = 0
    count = 100
    while True:
        api_url = (
            f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?startTime={start_time}&endTime={end_time}&type=ranked"
            f"&start={start}&count={count}&api_key={api_key}"
        )
        response = requests.get(api_url)

        # Back off and retry if we hit the rate limit
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            print(f"Rate limited, sleeping {retry_after}s...")
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        batch = response.json()

        if not batch:          # empty list -> no more matches
            break

        matches_in_year.extend(batch)
        start += count          # move to the next page

        if len(batch) < count:  # last (partial) page
            break

        time.sleep(1.2)         # stay under the dev key rate limit

    return matches_in_year

def check_table(matches_list):
    if not matches_list:
        print("No matches to process.")
        return

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            # Pull the match_ids already stored so we only insert new ones
            cur.execute("SELECT match_id FROM matches")
            existing = {row[0] for row in cur.fetchall()}

            # dict.fromkeys drops duplicates within the batch while keeping order
            new_matches = [
                m for m in dict.fromkeys(matches_list) if m not in existing
            ]

            if not new_matches:
                print("All matches already in the database, nothing to insert.")
                return

            # Bulk insert the missing match_ids (id is an identity column).
            # ON CONFLICT guards against the unique constraint if a concurrent
            # run inserted the same match_id between our SELECT and INSERT.
            cur.executemany(
                "INSERT INTO matches (match_id) VALUES (%s) "
                "ON CONFLICT (match_id) DO NOTHING",
                [(m,) for m in new_matches],
            )
        conn.commit()

    print(
        f"Inserted {len(new_matches)} new matches "
        f"({len(matches_list) - len(new_matches)} already present)."
    )

if __name__ == "__main__":
    all_matches = find_all_matches(2026)
    print(f"Found {len(all_matches)} matches")
    check_table(all_matches)

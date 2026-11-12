# Personal lol gameplay analytics

A personal data pipeline that pulls ranked League of Legends match history from the Riot Games API and loads it into a Postgres database for analysis. It goes beyond raw match stats to derive higher-level events — jungle ganks and back/recall timing — from the match timeline data.

[![Postgres](https://img.shields.io/badge/Postgres-%23316192.svg?logo=postgresql&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff)](#)
[![Power BI](https://custom-icon-badges.demolab.com/badge/Power%20BI-F1C912?logo=power-bi&logoColor=fff)](#)

## What it does

The pipeline runs in four stages, each a standalone script, each idempotent (safe to re-run — already-processed matches are skipped):

1. **[fill_matches_table.py](fill_matches_table.py)** — Calls the Riot Match-V5 API to list every ranked match ID for the tracked `puuid` in a given year, and inserts any new ones into the `matches` table.
2. **[fill_match_data.py](fill_match_data.py)** — For each match in `matches` not yet processed, fetches the full match JSON and flattens the tracked player's participant data (stats, challenges, objectives, summoner spell casts, Aftershock keystone values, etc.) into one row per match in `match_data`.
3. **[fill_match_ganks.py](fill_match_ganks.py)** — Fetches the match timeline and walks the `CHAMPION_KILL` event stream for the tracked player, recording every kill/assist participation into `match_ganks`. A participation counts as a "gank" when the victim's role is a laner (not jungle). Each row also snapshots the player's CS, level, and gold from the nearest timeline frame.
4. **[fill_match_recalls.py](fill_match_recalls.py)** — Fetches the timeline and clusters the player's `ITEM_PURCHASED` events (honoring `ITEM_UNDO`) into base visits, then classifies each visit as the opening buy, a post-death respawn shop, or an actual `recall`, storing results in `match_recalls`.

Scripts should be run in the order above, since later stages depend on data written by earlier ones (`match_data` and `matches` in particular).

## Requirements

- Python 3.13+
- A Postgres database
- A Riot Games API key ([developer portal](https://developer.riotgames.com/))

Python packages used: `requests`, `psycopg`, `python-dotenv`, `pandas`.

## Usage

```
python fill_matches_table.py
python fill_match_data.py
python fill_match_ganks.py
python fill_match_recalls.py
```

Each script prints progress as it goes and paces requests (~1.2s between calls) to stay under the Riot developer API key's rate limit. Note that Riot developer keys expire every 24 hours and need to be refreshed in `.env`.

## Database tables

- `matches` — match IDs discovered for the tracked player.
- `match_data` — one row per match with flattened participant stats/challenges/objectives.
- `match_ganks` — one row per kill/assist participation, flagged as gank or not.
- `match_recalls` — one row per base visit, classified as game start / post-death / recall.

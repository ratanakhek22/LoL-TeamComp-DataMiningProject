"""Crawl ranked solo queue drafts from the Riot API into SQLite.

Pipeline:
  1. Get Master / Grandmaster / Challenger players for a platform (League-V4).
  2. For each player, get their recent ranked solo match IDs (Match-V5).
  3. Fetch each match once (deduplicated) and store draft-level fields only:
     champions, positions, bans, patch, duration, and winning team.

The crawl is resumable: players and matches already processed are recorded in
the database, so you can stop it (Ctrl+C) and run it again later.

Usage (from the repo root):
    python -m collect.riot_crawler --check
    python -m collect.riot_crawler --platform na1 --days 14
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

import requests

QUEUE_RANKED_SOLO = 420
MIN_DURATION_SECONDS = 300  # shorter games are remakes

PLATFORM_TO_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

TIER_ENDPOINTS = {
    "challenger": "challengerleagues",
    "grandmaster": "grandmasterleagues",
    "master": "masterleagues",
}

# Personal key limits: 20 requests / 1 s and 100 requests / 120 s.
# Slightly under the real limits for safety.
DEFAULT_LIMITS = [(19, 1.0), (95, 120.0)]


class ApiKeyError(RuntimeError):
    """The key is missing, expired, or not allowed to call this endpoint."""


class RateLimiter:
    """Sliding-window limiter that enforces several (count, seconds) windows."""

    def __init__(self, limits=None, clock=time.monotonic, sleep=time.sleep):
        self.limits = limits or DEFAULT_LIMITS
        self.clock = clock
        self.sleep = sleep
        self.history: deque[float] = deque()

    def wait(self) -> None:
        while True:
            now = self.clock()
            longest = max(seconds for _, seconds in self.limits)
            while self.history and now - self.history[0] >= longest:
                self.history.popleft()

            delay = 0.0
            for count, seconds in self.limits:
                in_window = [t for t in self.history if now - t < seconds]
                if len(in_window) >= count:
                    # wait until the oldest request in this window expires
                    delay = max(delay, seconds - (now - in_window[-count]))
            if delay <= 0:
                self.history.append(now)
                return
            self.sleep(delay + 0.01)


class RiotClient:
    """Thin Riot API client with rate limiting and retries.

    Each routing host (e.g. na1 and americas) gets its own limiter, since Riot
    enforces limits per routing value.
    """

    def __init__(self, api_key: str, session=None, limits=None,
                 clock=time.monotonic, sleep=time.sleep, max_retries: int = 8):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        self.max_retries = max_retries
        self.limiters: dict[str, RateLimiter] = {}
        self.request_count = 0

    def _limiter(self, host: str) -> RateLimiter:
        if host not in self.limiters:
            self.limiters[host] = RateLimiter(self.limits, self.clock, self.sleep)
        return self.limiters[host]

    def get(self, host: str, path: str, params: dict | None = None):
        url = f"https://{host}.api.riotgames.com{path}"
        for attempt in range(self.max_retries):
            self._limiter(host).wait()
            self.request_count += 1
            try:
                resp = self.session.get(
                    url, params=params, headers={"X-Riot-Token": self.api_key}, timeout=30
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Wi-Fi blips, dropped connections, timeouts: wait and try again
                backoff = min(5 * 2 ** attempt, 120)
                print(f"  network error ({type(e).__name__}), retrying in {backoff}s",
                      file=sys.stderr)
                self.sleep(backoff)
                continue
            status = resp.status_code
            if status == 200:
                return resp.json()
            if status == 404:
                return None
            if status in (401, 403):
                raise ApiKeyError(
                    f"Riot API returned {status} for {path}. Your key may be "
                    "missing, expired (dev keys expire every 24h), or not approved."
                )
            if status == 429:
                retry_after = float(resp.headers.get("Retry-After", 10))
                print(f"  rate limited, waiting {retry_after:.0f}s", file=sys.stderr)
                self.sleep(retry_after)
                continue
            if status >= 500:
                backoff = 2 ** attempt
                print(f"  server error {status}, retrying in {backoff}s", file=sys.stderr)
                self.sleep(backoff)
                continue
            raise RuntimeError(f"Unexpected status {status} for {path}: {resp.text[:200]}")
        raise RuntimeError(f"Gave up on {path} after {self.max_retries} attempts")


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid      TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    tier       TEXT NOT NULL,
    crawled_at INTEGER            -- NULL until we've pulled their match IDs
);
CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    status        TEXT NOT NULL,  -- 'ok', 'remake', 'wrong_patch', 'missing', 'wrong_queue'
    game_version  TEXT,
    patch         TEXT,
    game_creation INTEGER,         -- epoch ms
    duration      INTEGER,         -- seconds
    winning_team  INTEGER          -- 100 (blue) or 200 (red)
);
CREATE TABLE IF NOT EXISTS picks (
    match_id      TEXT NOT NULL,
    slot          INTEGER NOT NULL, -- 0-9, participant order
    team_id       INTEGER NOT NULL,
    position      TEXT,             -- TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY ('' if unknown)
    champion_id   INTEGER NOT NULL,
    champion_name TEXT,
    win           INTEGER NOT NULL,
    PRIMARY KEY (match_id, slot)
);
CREATE TABLE IF NOT EXISTS bans (
    match_id    TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    pick_turn   INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,   -- -1 means no ban
    PRIMARY KEY (match_id, team_id, pick_turn)
);
CREATE INDEX IF NOT EXISTS idx_matches_patch ON matches(patch);
"""


def open_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # long timeout so crawls for different regions can share one database file
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def patch_of(game_version: str) -> str:
    """'15.21.715.4417' -> '15.21'"""
    parts = (game_version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def parse_match(data: dict) -> dict:
    """Pull draft-level fields out of a Match-V5 response. Stores no player IDs."""
    info = data["info"]
    picks = []
    for slot, p in enumerate(info["participants"]):
        picks.append({
            "slot": slot,
            "team_id": p["teamId"],
            "position": p.get("teamPosition") or "",
            "champion_id": p["championId"],
            "champion_name": p.get("championName"),
            "win": int(bool(p["win"])),
        })
    bans = []
    winning_team = None
    for team in info["teams"]:
        if team.get("win"):
            winning_team = team["teamId"]
        for b in team.get("bans", []):
            bans.append({"team_id": team["teamId"], "pick_turn": b["pickTurn"],
                         "champion_id": b["championId"]})
    return {
        "game_version": info.get("gameVersion", ""),
        "patch": patch_of(info.get("gameVersion", "")),
        "game_creation": info.get("gameCreation"),
        "duration": info.get("gameDuration", 0),
        "queue_id": info.get("queueId"),
        "early_surrender": any(p.get("gameEndedInEarlySurrender") for p in info["participants"]),
        "winning_team": winning_team,
        "picks": picks,
        "bans": bans,
    }


def save_match(conn, match_id: str, platform: str, parsed: dict | None,
               patch_filter: str | None) -> str:
    """Store a match (or a marker that it was skipped). Returns the status."""
    if parsed is None:
        status = "missing"
    elif parsed["queue_id"] != QUEUE_RANKED_SOLO:
        status = "wrong_queue"
    elif parsed["duration"] < MIN_DURATION_SECONDS or parsed["early_surrender"]:
        status = "remake"
    elif patch_filter and parsed["patch"] != patch_filter:
        status = "wrong_patch"
    else:
        status = "ok"

    with conn:
        if status != "ok":
            conn.execute(
                "INSERT OR IGNORE INTO matches (match_id, platform, status, patch) VALUES (?,?,?,?)",
                (match_id, platform, status, parsed["patch"] if parsed else None),
            )
            return status
        conn.execute(
            "INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?,?,?,?)",
            (match_id, platform, status, parsed["game_version"], parsed["patch"],
             parsed["game_creation"], parsed["duration"], parsed["winning_team"]),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO picks VALUES (?,?,?,?,?,?,?)",
            [(match_id, p["slot"], p["team_id"], p["position"], p["champion_id"],
              p["champion_name"], p["win"]) for p in parsed["picks"]],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO bans VALUES (?,?,?,?)",
            [(match_id, b["team_id"], b["pick_turn"], b["champion_id"]) for b in parsed["bans"]],
        )
    return status


# ---------------------------------------------------------------- crawl steps

def seed_players(client: RiotClient, conn, platform: str, tiers: list[str]) -> int:
    added = 0
    for tier in tiers:
        data = client.get(platform, f"/lol/league/v4/{TIER_ENDPOINTS[tier]}/by-queue/RANKED_SOLO_5x5")
        entries = (data or {}).get("entries", [])
        rows = [(e["puuid"], platform, tier) for e in entries if e.get("puuid")]
        with conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO players (puuid, platform, tier) VALUES (?,?,?)", rows)
            added += conn.total_changes - before
        print(f"{tier}: {len(entries)} players")
    return added


def match_ids_for(client: RiotClient, region: str, puuid: str, count: int,
                  start_time: int | None) -> list[str]:
    params = {"queue": QUEUE_RANKED_SOLO, "type": "ranked", "start": 0, "count": count}
    if start_time:
        params["startTime"] = start_time
    return client.get(region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params) or []


def crawl(client: RiotClient, conn, platform: str, *, tiers, matches_per_player: int,
          days: float | None, patch: str | None, max_matches: int | None,
          max_players: int | None, clock=time.time) -> dict:
    region = PLATFORM_TO_REGION[platform]
    start_time = int(clock() - days * 86400) if days else None

    has_players = conn.execute(
        "SELECT 1 FROM players WHERE platform=? LIMIT 1", (platform,)).fetchone()
    if not has_players:
        print("Fetching ladder...")
        seed_players(client, conn, platform, tiers)

    placeholders = ",".join("?" * len(tiers))
    pending = [row[0] for row in conn.execute(
        f"SELECT puuid FROM players WHERE platform=? AND crawled_at IS NULL "
        f"AND tier IN ({placeholders}) ORDER BY rowid", (platform, *tiers))]
    if max_players:
        pending = pending[:max_players]
    print(f"{len(pending)} players left to crawl on {platform}")

    stats = {"players": 0, "ok": 0, "skipped": 0}
    started = clock()
    for puuid in pending:
        for match_id in match_ids_for(client, region, puuid, matches_per_player, start_time):
            if conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone():
                continue  # already seen via another player
            data = client.get(region, f"/lol/match/v5/matches/{match_id}")
            status = save_match(conn, match_id, platform, parse_match(data) if data else None, patch)
            stats["ok" if status == "ok" else "skipped"] += 1
            if stats["ok"] and stats["ok"] % 50 == 0 and status == "ok":
                rate = stats["ok"] / max(clock() - started, 1) * 3600
                print(f"  {stats['ok']} matches saved ({rate:.0f}/hour)")
            if max_matches and stats["ok"] >= max_matches:
                return stats
        with conn:
            conn.execute("UPDATE players SET crawled_at=? WHERE puuid=?", (int(clock()), puuid))
        stats["players"] += 1
    return stats


def summary(conn) -> str:
    rows = conn.execute(
        "SELECT patch, COUNT(*) FROM matches WHERE status='ok' GROUP BY patch ORDER BY patch DESC"
    ).fetchall()
    total = sum(n for _, n in rows)
    lines = [f"Database total: {total} usable matches"]
    lines += [f"  patch {p}: {n}" for p, n in rows[:5]]
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def load_api_key() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.environ.get("RIOT_API_KEY", "").strip()
    if not key or key == "your-key-here":
        sys.exit("RIOT_API_KEY is not set. Copy .env.example to .env and add your key.")
    return key


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Crawl ranked solo queue drafts into SQLite.")
    ap.add_argument("--platform", default="na1", choices=sorted(PLATFORM_TO_REGION))
    ap.add_argument("--tiers", default="challenger,grandmaster,master",
                    help="comma-separated: challenger,grandmaster,master")
    ap.add_argument("--matches-per-player", type=int, default=20)
    ap.add_argument("--days", type=float, default=14,
                    help="only matches from the last N days (0 = no limit)")
    ap.add_argument("--patch", help="keep only this patch, e.g. 15.21")
    ap.add_argument("--max-matches", type=int, help="stop after saving this many matches")
    ap.add_argument("--max-players", type=int, help="crawl at most this many players this run")
    ap.add_argument("--db", default="data/raw/ranked.db")
    ap.add_argument("--check", action="store_true", help="test the API key and exit")
    args = ap.parse_args(argv)

    tiers = [t.strip().lower() for t in args.tiers.split(",") if t.strip()]
    bad = [t for t in tiers if t not in TIER_ENDPOINTS]
    if bad:
        sys.exit(f"Unknown tier(s): {', '.join(bad)}")

    client = RiotClient(load_api_key())

    if args.check:
        try:
            data = client.get(args.platform, "/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5")
        except ApiKeyError as e:
            sys.exit(str(e))
        print(f"Key works. {len((data or {}).get('entries', []))} Challenger players on {args.platform}.")
        return

    conn = open_db(args.db)
    try:
        stats = crawl(client, conn, args.platform, tiers=tiers,
                      matches_per_player=args.matches_per_player,
                      days=args.days or None, patch=args.patch,
                      max_matches=args.max_matches, max_players=args.max_players)
        print(f"Done: {stats['ok']} new matches, {stats['skipped']} skipped, "
              f"{stats['players']} players crawled.")
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command to resume.")
    except ApiKeyError as e:
        print(f"\n{e}")
    finally:
        print(summary(conn))
        print(f"API requests this run: {client.request_count}")
        conn.close()


if __name__ == "__main__":
    main()

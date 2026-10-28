"""Tests for the crawler using a fake Riot API (no network, no key needed)."""

import pytest

from collect.riot_crawler import (
    ApiKeyError, RateLimiter, RiotClient, crawl, open_db, parse_match, patch_of,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


def make_match(match_id, duration=1800, queue=420, version="15.21.715.1", blue_wins=True):
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    participants = []
    for i in range(10):
        team = 100 if i < 5 else 200
        participants.append({
            "teamId": team, "teamPosition": positions[i % 5],
            "championId": i + 1, "championName": f"Champ{i + 1}",
            "win": (team == 100) == blue_wins, "puuid": f"secret-{i}",
            "gameEndedInEarlySurrender": False,
        })
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameVersion": version, "gameCreation": 1, "gameDuration": duration,
            "queueId": queue, "participants": participants,
            "teams": [
                {"teamId": 100, "win": blue_wins, "bans": [{"championId": 50, "pickTurn": 1}]},
                {"teamId": 200, "win": not blue_wins, "bans": [{"championId": 60, "pickTurn": 6}]},
            ],
        },
    }


class FakeSession:
    """Routes URLs to canned responses and records every request."""

    def __init__(self, players, ids_by_player, matches, script=None):
        self.players, self.ids_by_player, self.matches = players, ids_by_player, matches
        self.script = list(script or [])  # responses to return first, in order
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if self.script:
            return self.script.pop(0)
        if "challengerleagues" in url:
            return FakeResponse(200, {"entries": [{"puuid": p} for p in self.players]})
        if "leagues" in url:
            return FakeResponse(200, {"entries": []})
        if "/by-puuid/" in url:
            puuid = url.split("/by-puuid/")[1].split("/")[0]
            return FakeResponse(200, self.ids_by_player.get(puuid, []))
        match_id = url.rsplit("/", 1)[1]
        if match_id in self.matches:
            return FakeResponse(200, self.matches[match_id])
        return FakeResponse(404)


def client_for(session, clock):
    return RiotClient("test-key", session=session, clock=clock, sleep=clock.sleep)


def test_patch_of():
    assert patch_of("15.21.715.4417") == "15.21"


def test_parse_match_keeps_draft_fields_and_drops_player_ids():
    parsed = parse_match(make_match("NA1_1"))
    assert parsed["patch"] == "15.21"
    assert parsed["winning_team"] == 100
    assert len(parsed["picks"]) == 10
    assert parsed["picks"][4]["position"] == "UTILITY"
    assert len(parsed["bans"]) == 2
    assert "secret" not in str(parsed)


def test_rate_limiter_enforces_both_windows():
    clock = FakeClock()
    limiter = RateLimiter([(19, 1.0), (95, 120.0)], clock=clock, sleep=clock.sleep)
    for _ in range(200):
        limiter.wait()
    times = list(limiter.history)
    # no 1-second window holds more than 19 requests
    assert all(sum(1 for u in times if t <= u < t + 1.0) <= 19 for t in times)
    # no 120-second window holds more than 95 requests
    assert all(sum(1 for u in times if t <= u < t + 120.0) <= 95 for t in times)
    # 200 requests at 95 per 2 minutes needs at least 2 full windows
    assert clock.t >= 240


def test_retries_after_429():
    clock = FakeClock()
    session = FakeSession([], {}, {}, script=[
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert client_for(session, clock).get("na1", "/x") == {"ok": True}
    assert clock.t >= 7


def test_bad_key_raises():
    clock = FakeClock()
    session = FakeSession([], {}, {}, script=[FakeResponse(403)])
    with pytest.raises(ApiKeyError):
        client_for(session, clock).get("na1", "/x")


def test_crawl_dedupes_filters_and_resumes(tmp_path):
    clock = FakeClock()
    matches = {
        "NA1_1": make_match("NA1_1"),
        "NA1_2": make_match("NA1_2", blue_wins=False),
        "NA1_3": make_match("NA1_3", duration=200),        # remake
        "NA1_4": make_match("NA1_4", version="15.20.1.1"),  # other patch
        "NA1_5": make_match("NA1_5"),
    }
    ids = {"p1": ["NA1_1", "NA1_2", "NA1_3"], "p2": ["NA1_2", "NA1_4", "NA1_5", "NA1_9"]}
    session = FakeSession(["p1", "p2"], ids, matches)
    conn = open_db(tmp_path / "t.db")

    # first run stops after one player
    crawl(client_for(session, clock), conn, "na1", tiers=["challenger"],
          matches_per_player=20, days=None, patch="15.21", max_matches=None,
          max_players=1, clock=clock)
    # second run resumes with the remaining player
    stats = crawl(client_for(session, clock), conn, "na1", tiers=["challenger"],
                  matches_per_player=20, days=None, patch="15.21", max_matches=None,
                  max_players=None, clock=clock)
    assert stats["players"] == 1

    status = dict(conn.execute("SELECT match_id, status FROM matches"))
    assert status == {"NA1_1": "ok", "NA1_2": "ok", "NA1_3": "remake",
                      "NA1_4": "wrong_patch", "NA1_5": "ok", "NA1_9": "missing"}
    # NA1_2 appeared for both players but was fetched once
    assert sum(1 for c in session.calls if c.endswith("/NA1_2")) == 1
    assert conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 30
    assert conn.execute("SELECT winning_team FROM matches WHERE match_id='NA1_2'").fetchone()[0] == 200
    # every player is marked crawled
    assert conn.execute("SELECT COUNT(*) FROM players WHERE crawled_at IS NULL").fetchone()[0] == 0

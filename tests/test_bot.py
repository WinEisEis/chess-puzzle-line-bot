"""Offline tests: no LINE account or internet needed (Lichess is faked)."""
import base64
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import chess
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before the app is imported.
os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = ""      # dry-run: nothing is really sent
os.environ["DAILY_PUSH_ENABLED"] = "false"
os.environ["ADMIN_KEY"] = "k"
os.environ["BASE_URL"] = "https://example.test"

from fastapi.testclient import TestClient  # noqa: E402

from app import lichess, store  # noqa: E402
from app.board import render_png  # noqa: E402
from app.bot import line  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# Scholar's mate set-up: White to play Qxf7#
SCHOLAR = {"game": {"id": "g1", "pgn": "e4 e5 Bc4 Nc6 Qh5 Nf6"},
           "puzzle": {"id": "SCH01", "rating": 800, "initialPly": 5,
                      "solution": ["h5f7"], "themes": ["opening", "mateIn1", "oneMove"]}}
# Black to move: 4...Qg5 5.Nxf7 Qxg2
BLACK = {"game": {"id": "g2", "pgn": "e4 e5 Nf3 Nc6 Bc4 Nd4 Nxe5"},
         "puzzle": {"id": "BLK01", "rating": 1300, "initialPly": 6,
                    "solution": ["d8g5", "e5f7", "g5g2"], "themes": ["opening", "advantage"]}}


@pytest.fixture(autouse=True)
def fake_lichess(monkeypatch, tmp_path):
    object.__setattr__(settings, "data_dir", tmp_path)
    lichess._cache.clear()
    line.sent.clear()
    from app import bot
    bot._shown_at.clear()   # each test starts with no solution timers

    async def fake_get(path, params=None):
        if path == "/puzzle/daily":
            return SCHOLAR
        if path == "/puzzle/next":
            return BLACK if params["angle"] == "endgame" else SCHOLAR
        pid = path.rsplit("/", 1)[-1]
        return {"SCH01": SCHOLAR, "BLK01": BLACK}[pid]

    monkeypatch.setattr(lichess, "_get", fake_get)


def signed_post(client, events):
    body = json.dumps({"destination": "U0", "events": events})
    sig = base64.b64encode(hmac.new(b"test-secret", body.encode(), hashlib.sha256).digest()).decode()
    return client.post("/callback", content=body,
                       headers={"X-Line-Signature": sig, "Content-Type": "application/json"})


def ev(kind, **extra):
    base = {"type": kind, "mode": "active", "timestamp": 1, "webhookEventId": "x",
            "deliveryContext": {"isRedelivery": False},
            "source": {"type": "group", "groupId": "Cgroup1", "userId": "U1"},
            "replyToken": "rt"}
    base.update(extra)
    return base


# --------------------------------------------------------------------------- puzzle logic
def test_parse_and_solution():
    p = lichess.parse_puzzle(SCHOLAR)
    assert p.side_to_move == "White"
    assert p.solution_san() == "4. Qxf7#"
    assert p.last_move == "g8f6"

    b = lichess.parse_puzzle(BLACK)
    assert b.side_to_move == "Black"
    assert b.solution_san() == "4...Qg5 5. Nxf7 Qxg2"
    assert [mine for _, mine in b.solution_steps()] == [True, False, True]


def test_render_png():
    png = render_png(lichess.parse_puzzle(BLACK).fen, "f3e5")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- webhook
def test_bad_signature_rejected():
    with TestClient(app) as c:
        r = c.post("/callback", content="{}", headers={"X-Line-Signature": "nope"})
        assert r.status_code == 400


def test_join_registers_group_and_shows_menu():
    with TestClient(app) as c:
        assert signed_post(c, [ev("join")]).status_code == 200
    assert store.all_groups() == ["Cgroup1"]
    kind, _, msgs = line.sent[-1]
    assert kind == "reply" and len(msgs) == 2


def test_category_button_returns_puzzle_card():
    with TestClient(app) as c:
        signed_post(c, [ev("postback", postback={"data": "a=new&t=endgame"})])
    msg = line.sent[-1][2][0]
    d = msg.to_dict()
    assert "Black to move" in d["altText"]
    hero = d["contents"]["hero"]
    assert hero["url"] == "https://example.test/board/BLK01.png"
    labels = json.dumps(d)
    for name in ("Opening", "Middlegame", "Endgame", "Show solution"):
        assert name in labels


def test_solution_button():
    with TestClient(app) as c:
        signed_post(c, [ev("postback", postback={"data": "a=sol&id=BLK01"})])
    assert "Qg5 5. Nxf7 Qxg2" in line.sent[-1][2][0].alt_text


def _say(c, text):
    signed_post(c, [ev("message", message={"type": "text", "id": "1", "text": text,
                                            "quoteToken": "q"})])


def test_text_commands_need_prefix():
    with TestClient(app) as c:
        for plain in ("opening", "endgame", "menu", "let's play the endgame", "/chessboard"):
            _say(c, plain)
        assert line.sent == []                       # plain chat is ignored

        _say(c, "/chess endgame")
        assert "Black to move" in line.sent[-1][2][0].alt_text
        _say(c, "/CHESS")                            # menu, case-insensitive
        assert "menu" in line.sent[-1][2][0].alt_text.lower() or "pick" in line.sent[-1][2][0].alt_text
        _say(c, "/หมากรุก ท้ายเกม")                    # Thai prefix + Thai alias
        assert "Black to move" in line.sent[-1][2][0].alt_text
        _say(c, "/chess xyz")                        # unknown → help + menu
        assert len(line.sent[-1][2]) == 2
    assert len(line.sent) == 4


def test_board_image_endpoint():
    with TestClient(app) as c:
        r = c.get("/board/SCH01.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_admin_push_daily():
    store.add_group("Cg1")
    store.add_group("Cg2")
    with TestClient(app) as c:
        assert c.post("/admin/push-daily").status_code == 403
        r = c.post("/admin/push-daily", headers={"X-Admin-Key": "k"})
    assert r.json()["sent"] == 2
    assert [s[0] for s in line.sent] == ["push", "push"]


def test_leave_unregisters():
    store.add_group("Cgroup1")
    with TestClient(app) as c:
        e = ev("leave")
        e.pop("replyToken")
        signed_post(c, [e])
    assert store.all_groups() == []


def test_stateless_token_is_issued_and_renewed(monkeypatch):
    import asyncio
    from app import bot

    calls = []

    async def fake_issue(cid, secret):
        calls.append((cid, secret))
        return f"tok{len(calls)}", 900

    monkeypatch.setattr(bot, "issue_stateless_token", fake_issue)
    object.__setattr__(settings, "channel_id", "123")
    l = bot.Line()
    try:
        asyncio.run(l.start())
        asyncio.run(l._ensure_token())
        asyncio.run(l._ensure_token())          # still valid → no new request
        assert calls == [("123", "test-secret")] and l._config.access_token == "tok1"
        l._expires_at = 1.0                     # pretend it expired
        asyncio.run(l._ensure_token())
        assert l._config.access_token == "tok2"
    finally:
        object.__setattr__(settings, "channel_id", "")


# --------------------------------------------------------------------------- solution lock
def test_solution_locked_for_10_minutes(monkeypatch):
    from app import bot
    bot._shown_at.clear()
    now = [1_000_000.0]
    monkeypatch.setattr(bot.time, "time", lambda: now[0])
    with TestClient(app) as c:
        signed_post(c, [ev("postback", postback={"data": "a=new&t=endgame"})])   # posts BLK01
        signed_post(c, [ev("postback", postback={"data": "a=sol&id=BLK01"})])
        assert "10 นาที" in line.sent[-1][2][0].text and "เหลืออีก 10" in line.sent[-1][2][0].text
        now[0] += 7 * 60 + 5
        signed_post(c, [ev("postback", postback={"data": "a=sol&id=BLK01"})])
        assert "เหลืออีก 3" in line.sent[-1][2][0].text
        now[0] += 3 * 60
        signed_post(c, [ev("postback", postback={"data": "a=sol&id=BLK01"})])
        assert "Qg5" in line.sent[-1][2][0].alt_text                            # unlocked
        # Another group never saw it via the bot → not locked there
        e = ev("postback", postback={"data": "a=sol&id=SCH01"})
        e["source"]["groupId"] = "Cother"
        signed_post(c, [e])
        assert "Qxf7#" in line.sent[-1][2][0].alt_text


# --------------------------------------------------------------------------- play page API
def test_play_api_hides_solution_and_checks_moves():
    with TestClient(app) as c:
        assert "__PUZZLE_ID__" not in c.get("/play/BLK01").text
        info = c.get("/api/puzzle/BLK01").json()
        assert "solution" not in info and info["fen"].split()[1] == "b"
        r = c.post("/api/puzzle/BLK01/check", json={"line": ["d8g5"]}).json()
        assert r == {"correct": True, "reply": "e5f7", "done": False}
        r = c.post("/api/puzzle/BLK01/check", json={"line": ["d8h4"]}).json()
        assert r["correct"] is False
        r = c.post("/api/puzzle/BLK01/check", json={"line": ["d8g5", "e5f7", "g5g2"]}).json()
        assert r["done"] is True
        assert c.get("/api/puzzle/BLK01/hint?ply=0").json() == {"square": "d8"}
        assert c.get("/static/pieces/wK.png").status_code == 200
        assert c.get("/static/chess.js").status_code == 200


# --------------------------------------------------------------------------- top-game puzzles
LEGAL_PGN = """[Event "Test Open"]
[Round "5"]
[White "Legal, Sire de"]
[Black "Saint Brie"]
[Result "1-0"]
[ChapterURL "https://lichess.org/broadcast/test/round-5/abcd1234/efgh5678"]

1. e4 e5 2. Nf3 d6 3. Bc4 Bg4 4. Nc3 g6 5. Nxe5 Bxd1 6. Bxf7+ Ke7 7. Nd5# 1-0
"""


def _top_puzzle():
    return lichess.Puzzle(
        id="top0123456789", rating=0, themes=[],
        fen="rn1qkbnr/ppp2p1p/3p2p1/4N3/2B1P3/2N5/PPPP1PPP/R1BbK2R w KQkq - 0 6",
        last_move="g4d1", solution=["c4f7", "e8e7", "c3d5"], game_url="https://x",
        source="Legal – Saint Brie · Test Open 2026 · Round 5",
        note="In the game, Legal found it! ✅", url="https://x")


def test_pick_rounds_prefers_latest_finished_top_tier():
    import json as _j
    from app import topgames
    now = 1_800_000_000_000
    day = 86_400_000
    lines = [
        {"tour": {"name": "Elite Cup", "tier": 5},
         "rounds": [{"id": "r1", "name": "Round 1", "startsAt": now - 3 * day, "finished": True},
                    {"id": "r2", "name": "Round 2", "startsAt": now - 1 * day, "finished": True},
                    {"id": "r3", "name": "Round 3", "startsAt": now + 1 * day}]},
        {"tour": {"name": "Club Open", "tier": 3},
         "rounds": [{"id": "c1", "name": "R1", "startsAt": now - day, "finished": True}]},
        {"tour": {"name": "Old Masters", "tier": 5},
         "rounds": [{"id": "o1", "name": "R9", "startsAt": now - 40 * day, "finished": True}]},
    ]
    picked = topgames.pick_rounds("\n".join(_j.dumps(x) for x in lines), now_ms=now)
    assert [p["id"] for p in picked] == ["r2"]
    assert picked[0]["tour"] == "Elite Cup"


def test_top_command_and_waiting_message(monkeypatch):
    from app import topgames
    monkeypatch.setattr(topgames, "refresh_in_background", lambda: None)
    monkeypatch.setattr(topgames, "_latest", None)
    with TestClient(app) as c:
        _say(c, "/chess top")
        assert "Stockfish" in line.sent[-1][2][0].text          # not ready yet
        monkeypatch.setattr(topgames, "_latest", lichess.pin(_top_puzzle()))
        signed_post(c, [ev("postback", postback={"data": "a=top"})])
        d = line.sent[-1][2][0].to_dict()
        assert "Top Game" in d["altText"]
        assert "Legal – Saint Brie" in json.dumps(d, ensure_ascii=False)
        assert c.get("/board/top0123456789.png").status_code == 200
        assert c.get("/api/puzzle/top0123456789").json()["source"].startswith("Legal")
        r = c.post("/api/puzzle/top0123456789/check", json={"line": ["c4f7"]}).json()
        assert r["reply"] == "e8e7"


def test_daily_push_bundles_top_puzzle_in_one_request(monkeypatch):
    from app import topgames
    monkeypatch.setattr(topgames, "refresh_in_background", lambda: None)
    monkeypatch.setattr(topgames, "_latest", lichess.pin(_top_puzzle()))
    monkeypatch.setattr(topgames, "_pushed", "")
    store.add_group("Cg1")
    with TestClient(app) as c:
        c.post("/admin/push-daily", headers={"X-Admin-Key": "k"})
        assert len(line.sent) == 1 and len(line.sent[0][2]) == 2   # 1 push, 2 cards
        line.sent.clear()
        c.post("/admin/push-daily", headers={"X-Admin-Key": "k"})
        assert len(line.sent[0][2]) == 1                           # same top puzzle not repeated


@pytest.mark.skipif(not __import__("app.engine", fromlist=["x"]).find_stockfish(),
                    reason="Stockfish not installed")
def test_stockfish_finds_legals_mate(monkeypatch):
    from app import topgames
    from app.engine import Engine, find_stockfish
    object.__setattr__(settings, "top_min_ply", 4)   # the sample game is very short
    try:
        games = [({"id": "abcd1234", "tour": "Test Open 2026", "round": "Round 5"}, g)
                 for g in topgames.parse_games(LEGAL_PGN)]
        with Engine(find_stockfish()) as e:
            p, _ = topgames.find_best_puzzle(games, e)
        assert p.solution == ["c4f7", "e8e7", "c3d5"]
        assert "found it" in p.note
    finally:
        object.__setattr__(settings, "top_min_ply", 14)


def test_master_game_filters():
    from app import topgames
    assert topgames._is_classical_event("Norway Chess 2023")
    assert topgames._is_classical_event("Tata Steel Masters")
    for fast in ("World Blitz", "Chessable Masters Div 1", "Titled Tuesday", "Carlsen Tour Final",
                 "Champions Chess Tour Airthings Masters", "Casual Rapid game"):
        assert not topgames._is_classical_event(fast), fast
    assert not topgames._is_classical_event("Some Open", "180+2")
    assert topgames._is_classical_event("Some Open", "5400+30")
    pages = [{"topGames": [
        {"id": "aaa", "white": {"name": "Carlsen, M.", "rating": 2863},
         "black": {"name": "Caruana, F.", "rating": 2835}, "year": 2019, "winner": None},
        {"id": "bbb", "white": {"name": "Ding, L.", "rating": 2805},
         "black": {"name": "Nepo, I.", "rating": 2795}, "year": 2023, "winner": "white"},
        {"id": "ccc", "white": {"name": "Club, A.", "rating": 2350},
         "black": {"name": "Club, B.", "rating": 2400}, "year": 2020, "winner": "black"},
        {"id": "ddd", "white": {"name": "Kasparov, G.", "rating": 2851},
         "black": {"name": "Topalov, V.", "rating": 2700}, "year": 1999, "winner": "white"},
    ]}, {"topGames": [{"id": "bbb", "white": {"rating": 2805}, "black": {"rating": 2795},
                       "year": 2023, "winner": "white"}]}]
    picked = topgames.pick_master_games(pages, skip={"m:aaa"})
    assert [g["id"] for g in picked] == ["bbb"]          # 2700+, since 2005, not skipped, no dupes


def test_only_move_rule():
    from app import topgames
    m = chess.Move.from_uci("e2e4")
    assert topgames._winning_only_move([(m, 9997), (m, 600)])        # short mate, others don't mate
    assert not topgames._winning_only_move([(m, 9997), (m, 9990)])   # several mates
    assert topgames._winning_only_move([(m, 450), (m, 0)])           # clearly the only win
    assert not topgames._winning_only_move([(m, 1100), (m, 600)])    # both win anyway


def test_fetch_master_games_flow(monkeypatch):
    """Simulated Lichess explorer: walk the tree, keep 2700+ classical games, download PGNs."""
    import asyncio as _a
    import httpx
    from app import topgames

    pgns = {
        "g1": '[Event "Norway Chess 2023"]\n[White "Carlsen, M."]\n[Black "Caruana, F."]\n'
              '[Result "1-0"]\n[Date "2023.06.01"]\n\n1. e4 e5 2. Nf3 Nc6 1-0\n',
        "g2": '[Event "World Blitz 2023"]\n[White "Carlsen, M."]\n[Black "Nakamura, H."]\n'
              '[Result "0-1"]\n\n1. d4 d5 0-1\n',
    }
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/masters":
            return httpx.Response(200, json={
                "moves": [{"uci": "e2e4", "white": 10, "draws": 5, "black": 3}],
                "topGames": [
                    {"id": "g1", "white": {"name": "Carlsen, M.", "rating": 2853},
                     "black": {"name": "Caruana, F.", "rating": 2766}, "year": 2023, "winner": "white"},
                    {"id": "g2", "white": {"name": "Carlsen, M.", "rating": 2853},
                     "black": {"name": "Nakamura, H.", "rating": 2780}, "year": 2023, "winner": "black"}]})
        gid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=pgns[gid])

    real = httpx.AsyncClient
    monkeypatch.setattr(topgames.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def no_sleep(_):
        return None
    monkeypatch.setattr(topgames.asyncio, "sleep", no_sleep)
    object.__setattr__(settings, "top_walks", 1)
    try:
        games = _a.run(topgames.fetch_master_games())
    finally:
        object.__setattr__(settings, "top_walks", 8)
    assert [m["id"] for m, _ in games] == ["g1"]              # blitz game dropped
    meta, g = games[0]
    assert meta["url"] == "https://lichess.org/g1" and meta["key"] == "m:g1"
    assert g.headers["White"] == "Carlsen, M."
    assert any(p.startswith("/master/pgn/") for p in calls)

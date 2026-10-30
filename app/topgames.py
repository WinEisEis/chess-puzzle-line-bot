"""Puzzles from the latest top-level games (not the Lichess puzzle database).

1. Ask Lichess Broadcasts (free, no key) for recent official top tournaments and download the
   games of their latest finished rounds.
2. Let Stockfish look through the decisive games for the moment where the winner had ONE move
   that wins (and every other move doesn't) — the same idea Lichess uses to make its puzzles.
3. Turn the best moment into a Puzzle, so it works with the card, board page and 10-min lock.

Runs once a day in the background (TOP_REFRESH_TIME) because the free Render server is slow.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import time
from dataclasses import asdict, dataclass

import chess
import chess.pgn
import httpx

from . import lichess
from .config import settings
from .engine import Engine, find_stockfish

log = logging.getLogger("topgames")

API = "https://lichess.org/api"
MATE = 9000


# --------------------------------------------------------------------------- Lichess broadcasts
def pick_rounds(ndjson: str, now_ms: int | None = None) -> list[dict]:
    """From /api/broadcast NDJSON, choose the latest finished rounds of top-tier tournaments."""
    now_ms = now_ms or int(time.time() * 1000)
    oldest = now_ms - settings.top_lookback_days * 86_400_000
    picked = []
    for line in ndjson.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        tour = d.get("tour", {})
        tier = tour.get("tier")
        if isinstance(tier, int) and tier < settings.top_min_tier:
            continue
        rounds = [r for r in d.get("rounds", []) if r.get("id")]
        done = [r for r in rounds
                if (r.get("finished") or r.get("finishedAt"))
                and oldest <= (r.get("startsAt") or r.get("createdAt") or 0) <= now_ms]
        if not done:
            continue
        latest = max(done, key=lambda r: r.get("startsAt") or r.get("createdAt") or 0)
        picked.append({"id": latest["id"], "round": latest.get("name", ""),
                       "tour": tour.get("name", "Top tournament"),
                       "at": latest.get("startsAt") or latest.get("createdAt") or 0})
    picked.sort(key=lambda r: r["at"], reverse=True)
    return picked[: settings.top_max_rounds]


def parse_games(pgn_text: str) -> list[chess.pgn.Game]:
    stream, games = io.StringIO(pgn_text), []
    while True:
        try:
            g = chess.pgn.read_game(stream)
        except Exception:
            break
        if g is None:
            break
        games.append(g)
    return games


async def fetch_recent_games() -> list[tuple[dict, chess.pgn.Game]]:
    headers = {"User-Agent": "chess-puzzle-line-bot/1.0"}
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        r = await client.get(f"{API}/broadcast", params={"nb": 40},
                             headers={"Accept": "application/x-ndjson"})
        r.raise_for_status()
        rounds = pick_rounds(r.text)
        out = []
        for rd in rounds:
            pr = await client.get(f"{API}/broadcast/round/{rd['id']}.pgn")
            if pr.status_code != 200:
                continue
            out += [(rd, g) for g in parse_games(pr.text)]
    log.info("Fetched %d games from %d rounds: %s", len(out), len(rounds),
             ", ".join(f"{r['tour']} {r['round']}" for r in rounds))
    return out


# --------------------------------------------------------------------------- finding the moment
@dataclass
class Candidate:
    fen: str
    last_move: str | None
    best: str
    played: str
    score: int


def _surname(name: str) -> str:
    return name.split(",")[0].strip() or name


def _winning_only_move(top: list[tuple[chess.Move, int]]) -> bool:
    """True if the best move wins clearly and the 2nd-best doesn't."""
    if len(top) < 2:
        return False
    (_, s1), (_, s2) = top[0], top[1]
    return s1 >= 200 and s2 < 150 and s1 - s2 >= 200


def scan_game(game: chess.pgn.Game, eng: Engine, budget: list[int]) -> list[Candidate]:
    """Look at the winner's moves; keep positions where a winning only-move suddenly appears."""
    result = game.headers.get("Result", "*")
    if result not in ("1-0", "0-1"):
        return []
    winner = chess.WHITE if result == "1-0" else chess.BLACK
    board = game.board()
    prev_best: int | None = None
    found = []
    for ply, move in enumerate(game.mainline_moves()):
        if board.turn == winner and ply >= settings.top_min_ply and budget[0] > 0:
            budget[0] -= 1
            top = eng.top_moves(board, settings.top_scan_nodes, multipv=2)
            if top:
                s1 = top[0][1]
                last = board.peek() if board.move_stack else None
                recapture = False
                if last and board.is_capture(top[0][0]) and top[0][0].to_square == last.to_square:
                    before = board.copy(stack=True)
                    before.pop()
                    recapture = before.is_capture(last)   # plain "take back" — too obvious
                swing = s1 - prev_best if prev_best is not None else 999
                if _winning_only_move(top) and swing >= 250 and not recapture:
                    s2 = top[1][1]
                    score = min(s1, 1500) - max(s2, -300)
                    score += 300 if top[0][0] == move else 0      # the player really found it
                    score += 200 if s1 >= MATE else 0             # mates are fun
                    found.append(Candidate(board.fen(), last.uci() if last else None,
                                           top[0][0].uci(), move.uci(), score))
                prev_best = s1
        board.push(move)
    return found


def build_line(fen: str, eng: Engine) -> list[str] | None:
    """Re-check with a deeper search and extend while the winner keeps having only-moves."""
    board = chess.Board(fen)
    top = eng.top_moves(board, settings.top_verify_nodes, multipv=2)
    if not _winning_only_move(top):
        return None
    line = [top[0][0].uci()]
    board.push(top[0][0])
    for _ in range(2):                        # at most 3 moves for the solver
        if board.is_game_over():
            break
        reply = eng.top_moves(board, settings.top_verify_nodes // 2, multipv=1)
        if not reply:
            break
        nxt = board.copy()
        nxt.push(reply[0][0])
        if nxt.is_game_over():
            break
        top = eng.top_moves(nxt, settings.top_verify_nodes, multipv=2)
        if not _winning_only_move(top):
            break
        line += [reply[0][0].uci(), top[0][0].uci()]
        board = nxt
        board.push(top[0][0])
    return line


def make_puzzle(rd: dict, game: chess.pgn.Game, cand: Candidate, line: list[str]) -> lichess.Puzzle:
    h = game.headers
    white, black = _surname(h.get("White", "White")), _surname(h.get("Black", "Black"))
    winner = white if chess.Board(cand.fen).turn == chess.WHITE else black
    event = rd.get("tour") or h.get("Event", "")
    rnd = rd.get("round") or (f"Round {h['Round']}" if h.get("Round") else "")
    source = " · ".join(x for x in (f"{white} – {black}", event, rnd) if x)
    note = (f"In the game, {winner} found it! ✅" if cand.played == line[0]
            else f"In the game, {winner} missed it 😮")
    url = next((h[k] for k in ("ChapterURL", "GameURL", "Site") if h.get(k, "").startswith("http")),
               f"https://lichess.org/broadcast/-/-/{rd.get('id', '')}")
    pid = "top" + hashlib.sha1((cand.fen + " ".join(line)).encode()).hexdigest()[:10]
    return lichess.Puzzle(id=pid, rating=0, themes=[], fen=cand.fen, last_move=cand.last_move,
                          solution=line, game_url=url, source=source, note=note, url=url)


def game_key(rd: dict, game: chess.pgn.Game) -> str:
    h = game.headers
    return f"{rd.get('id')}|{h.get('White')}|{h.get('Black')}"


def find_best_puzzle(games: list[tuple[dict, chess.pgn.Game]], eng: Engine,
                     skip: set[str] = frozenset()) -> tuple[lichess.Puzzle, str] | None:
    budget = [settings.top_max_positions]
    cands = []
    for rd, g in games:
        if game_key(rd, g) in skip:
            continue
        cands += [(c, rd, g) for c in scan_game(g, eng, budget)]
    cands.sort(key=lambda x: x[0].score, reverse=True)
    for cand, rd, g in cands[:5]:              # deeper check on the most promising few
        line = build_line(cand.fen, eng)
        if line:
            return make_puzzle(rd, g, cand, line), game_key(rd, g)
    return None


# --------------------------------------------------------------------------- state & refresh
_latest: lichess.Puzzle | None = None
_latest_at = 0.0
_used: list[str] = []          # games already turned into puzzles (don't repeat them)
_pushed = ""                   # id of the top puzzle last included in the daily post
_lock = asyncio.Lock()


def _state_file():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "top_puzzle.json"


def load_saved() -> None:
    global _latest, _latest_at, _used, _pushed
    try:
        d = json.loads(_state_file().read_text("utf-8"))
        _latest = lichess.pin(lichess.Puzzle(**d["puzzle"]))
        _latest_at, _used, _pushed = d.get("at", 0.0), d.get("used", []), d.get("pushed", "")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass


def _save() -> None:
    try:
        _state_file().write_text(json.dumps(
            {"puzzle": asdict(_latest), "at": _latest_at, "used": _used[-200:], "pushed": _pushed},
            ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        log.exception("Could not save top puzzle")


def last_pushed() -> str:
    return _pushed


def mark_pushed(puzzle_id: str) -> None:
    global _pushed
    _pushed = puzzle_id
    if _latest:
        _save()


def latest() -> lichess.Puzzle | None:
    return _latest if settings.top_enabled else None


def is_fresh() -> bool:
    return _latest is not None and time.time() - _latest_at < 20 * 3600


def is_running() -> bool:
    return _lock.locked()


async def refresh() -> lichess.Puzzle | None:
    """Fetch latest top games and pick a new puzzle. Takes a few minutes on a small server."""
    global _latest, _latest_at
    if _lock.locked() or not settings.top_enabled:
        return None
    async with _lock:
        path = find_stockfish()
        if not path:
            log.warning("Stockfish not found → top-game puzzles disabled "
                        "(Mac: brew install stockfish; Render: see scripts/get_stockfish.sh)")
            return None
        t0 = time.time()
        try:
            games = await fetch_recent_games()
        except Exception:
            log.exception("Could not fetch broadcast games")
            return None

        def work():
            with Engine(path) as eng:
                return find_best_puzzle(games, eng, set(_used))

        found = await asyncio.to_thread(work)
        if not found:
            log.warning("No suitable top-game position found (%.0fs)", time.time() - t0)
            return None
        puzzle, key = found
        _latest, _latest_at = lichess.pin(puzzle), time.time()
        _used.append(key)
        _save()
        log.info("New top-game puzzle %s: %s (%.0fs)", puzzle.id, puzzle.source, time.time() - t0)
        return puzzle


_tasks: set = set()


def refresh_in_background() -> None:
    if not _lock.locked():
        t = asyncio.get_running_loop().create_task(refresh())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)

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


# --------------------------------------------------------------------------- Lichess Masters DB
EXPLORER = "https://explorer.lichess.ovh"
FAST_EVENT_WORDS = ("blitz", "rapid", "bullet", "armageddon", "online", "speed", "960",
                    "fischer random", "freestyle", "titled tuesday", "banter", "simul", "blind",
                    "chessable", "champions chess tour", "meltwater", "airthings", "aimchess",
                    "goldmoney", "ftx", "crypto cup", "invitational", "opera euro", "skilling",
                    "new in chess classic", "julius baer", "global championship", "tour final",
                    "pro league", "lindores", "chess.com", "chess24", "lichess", "charity cup",
                    "oslo esports", "play magnus", "carlsen tour", "bundesliga blitz", "knockout")


def _is_classical_event(event: str, time_control: str = "") -> bool:
    e = (event or "").lower()
    if any(w in e for w in FAST_EVENT_WORDS):
        return False
    base = (time_control or "").split("+")[0].split("/")[-1]
    if base.isdigit() and int(base) < 3600:      # < 60 minutes per player → not classical
        return False
    return True


def pick_master_games(explorer_pages: list[dict], skip: set[str]) -> list[dict]:
    """From Masters-explorer responses, keep classical games with a 2700+ player."""
    seen, out = set(), []
    for page in explorer_pages:
        for g in page.get("topGames", []) + page.get("recentGames", []):
            gid = g.get("id")
            if not gid or gid in seen or f"m:{gid}" in skip:
                continue
            seen.add(gid)
            w, b = g.get("white", {}), g.get("black", {})
            top = max(w.get("rating") or 0, b.get("rating") or 0)
            if top < settings.top_min_elo or (g.get("year") or 0) < settings.top_since_year:
                continue
            out.append({"id": gid, "white": w.get("name", ""), "black": b.get("name", ""),
                        "rating": top, "year": g.get("year"), "winner": g.get("winner")})
    # decisive games first (more tactics), but keep some draws for "missed wins"
    out.sort(key=lambda x: (x["winner"] is None, -x["rating"]))
    return out


async def fetch_master_games(skip: set[str] = frozenset()) -> list[tuple[dict, chess.pgn.Game]]:
    """Random walk through the Masters opening tree (OTB games of titled players) and collect
    games played by the elite, then download their full PGNs."""
    import random
    headers = {"User-Agent": "chess-puzzle-line-bot/1.0"}
    if settings.lichess_token:
        headers["Authorization"] = f"Bearer {settings.lichess_token}"
    pages: list[dict] = []
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        for _ in range(settings.top_walks):
            play: list[str] = []
            for _depth in range(random.randint(6, 16)):
                r = await client.get(f"{EXPLORER}/masters",
                                     params={"play": ",".join(play), "since": settings.top_since_year,
                                             "moves": 12, "topGames": 15})
                if r.status_code == 429:
                    await asyncio.sleep(60)
                    continue
                r.raise_for_status()
                d = r.json()
                pages.append(d)
                moves = [m for m in d.get("moves", []) if m.get("uci")]
                if not moves:
                    break
                weights = [((m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)) or 1) ** 0.5
                           for m in moves]
                play.append(random.choices(moves, weights)[0]["uci"])
                await asyncio.sleep(0.7)      # be polite to Lichess
        picked = pick_master_games(pages, set(skip))
        out = []
        for meta in picked:
            if len(out) >= settings.top_games_per_run:
                break
            pgn = None
            for path in (f"/master/pgn/{meta['id']}", f"/masters/pgn/{meta['id']}"):
                r = await client.get(f"{EXPLORER}{path}")
                if r.status_code == 200 and r.text.strip():
                    pgn = r.text
                    break
            await asyncio.sleep(0.7)
            games = parse_games(pgn) if pgn else []
            if not games or not _is_classical_event(games[0].headers.get("Event", ""),
                                                    games[0].headers.get("TimeControl", "")):
                continue
            g = games[0]
            meta = {**meta, "tour": g.headers.get("Event", ""), "round": "",
                    "url": f"https://lichess.org/{meta['id']}", "key": f"m:{meta['id']}"}
            out.append((meta, g))
    log.info("Fetched %d classical master games (2700+) from %d explorer pages",
             len(out), len(pages))
    return out


# --------------------------------------------------------------------------- finding the moment
VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


@dataclass
class Candidate:
    fen: str
    last_move: str | None
    played: str
    priority: float
    kind: str                     # "turn" | "found" | "missed"


@dataclass
class Scored:
    line: list[str]
    score: int
    weak_found: bool
    tags: list[str]


def _surname(name: str) -> str:
    return name.split(",")[0].strip() or name


def win_pct(cp: int) -> float:
    """Lichess's formula: centipawns → winning chances (0–100%)."""
    import math
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * max(-2000, min(2000, cp)))) - 1)


def _winning_only_move(top: list[tuple[chess.Move, int]]) -> bool:
    """Only one move wins:
       • a short forced mate (≤ 5 moves) while no other move mates, or
       • best move gives ≥ 75% winning chances and the 2nd-best is ≥ 25 points worse."""
    if len(top) < 2:
        return False
    (_, s1), (_, s2) = top[0], top[1]
    if s1 >= MATE:
        return s1 >= 10000 - 10 and s2 < MATE
    return win_pct(s1) >= 75 and win_pct(s1) - win_pct(s2) >= 25


def scan_game(game: chess.pgn.Game, eng: Engine, budget: list[int]) -> list[Candidate]:
    """Quick pass over the whole game, then mark interesting moments:
       turn   – opponent just erred: side to move suddenly has a win
       found  – the player made a move a quick search didn't rate, and it turned out winning
                (exactly the kind of move that is hard to see)
       missed – a win was there but the player didn't take it
    """
    board = game.board()
    moves = list(game.mainline_moves())
    fens, ev = [], []                   # ev[i] = quick eval (White's view) before move i
    for i, mv in enumerate(moves + [None]):
        fens.append(board.fen())
        if i >= settings.top_min_ply - 1 and budget[0] > 0 and not board.is_game_over():
            budget[0] -= 1
            top = eng.top_moves(board, settings.top_scan_nodes, multipv=1)
            s = top[0][1] if top else 0
            ev.append(s if board.turn == chess.WHITE else -s)
        else:
            ev.append(None)
        if mv is not None:
            board.push(mv)

    def pov(i: int, white: bool) -> int | None:
        if i < 0 or i >= len(ev) or ev[i] is None:
            return None
        return ev[i] if white else -ev[i]

    out = []
    for i in range(settings.top_min_ply, len(moves)):
        white = fens[i].split()[1] == "w"
        now, before = pov(i, white), pov(i - 1, white)
        after1, after2 = pov(i + 1, white), pov(i + 2, white)
        if now is None:
            continue
        last = moves[i - 1].uci() if i > 0 else None
        kind, prio = None, 0.0
        # Elite classical games rarely swing wildly, so these thresholds are modest;
        # the deep check in score_candidate() is what decides.
        if (after1 is not None and after2 is not None and now <= 150
                and after2 >= 250 and after2 - now >= 150 and after1 >= 200):
            kind, prio = "found", 3 + min(after2 - now, 1500) / 1000
        elif now >= 150 and before is not None and now - before >= 120:
            if after1 is not None and after1 <= now - 150:
                kind, prio = "missed", 3 + min(now, 1500) / 1000
            else:
                kind, prio = "turn", 1 + min(now - before, 1500) / 1000
        if kind:
            out.append(Candidate(fens[i], last, moves[i].uci(), prio, kind))
    return out


def _is_free_capture(board: chess.Board, mv: chess.Move) -> bool:
    """Capturing an undefended piece (or a plain recapture) — too obvious for a puzzle."""
    if not board.is_capture(mv):
        return False
    if board.move_stack:
        last = board.peek()
        prev = board.copy(stack=True)
        prev.pop()
        if last.to_square == mv.to_square and prev.is_capture(last):
            return True                                   # recapture
    defended = board.is_attacked_by(not board.turn, mv.to_square)
    return not defended


def _is_sacrifice(board: chess.Board, mv: chess.Move) -> bool:
    """The moved piece can be taken and is worth more than what it captured."""
    piece = board.piece_at(mv.from_square)
    if not piece or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    taken = board.piece_at(mv.to_square)
    gain = VALUES[taken.piece_type] if taken else 0
    after = board.copy()
    after.push(mv)
    return after.is_attacked_by(after.turn, mv.to_square) and VALUES[piece.piece_type] > gain + 1


def build_line(board: chess.Board, first: chess.Move, eng: Engine) -> list[str]:
    """Extend the solution while the solver keeps having a single winning move (max 3)."""
    b = board.copy()
    line = [first.uci()]
    b.push(first)
    for _ in range(2):
        if b.is_game_over():
            break
        reply = eng.top_moves(b, settings.top_verify_nodes // 2, multipv=1)
        if not reply:
            break
        nxt = b.copy()
        nxt.push(reply[0][0])
        if nxt.is_game_over():
            break
        top = eng.top_moves(nxt, settings.top_verify_nodes, multipv=2)
        if not _winning_only_move(top):
            break
        line += [reply[0][0].uci(), top[0][0].uci()]
        b = nxt
        b.push(top[0][0])
    return line


def score_candidate(cand: Candidate, eng: Engine) -> Scored | None:
    """Deep check + difficulty score. None = not a good (or not a hard enough) puzzle."""
    board = chess.Board(cand.fen)
    if board.is_check() and board.legal_moves.count() <= 3:
        return None                                   # forced: almost no choice
    top = eng.top_moves(board, settings.top_verify_nodes, multipv=2)
    if not _winning_only_move(top):
        return None
    best, s1 = top[0]
    if _is_free_capture(board, best) and s1 < MATE:
        return None
    weak = eng.top_moves_depth(board, settings.top_weak_depth)   # "human blind spot" test
    weak_found = bool(weak) and weak[0][0] == best
    line = build_line(board, best, eng)
    solver_moves = (len(line) + 1) // 2

    tags, score = [], 0
    if not weak_found:
        score += 600; tags.append("deep")
    if _is_sacrifice(board, best):
        score += 350; tags.append("sacrifice")
    if not board.is_capture(best) and not board.gives_check(best):
        score += 250; tags.append("quiet")
    score += 250 * (solver_moves - 1)
    if solver_moves > 1:
        tags.append(f"{solver_moves}-move")
    if cand.played != best.uci():
        score += 250; tags.append("missed")
    if s1 >= MATE:
        score += 150; tags.append("mate")
    hard_enough = (not weak_found) or "sacrifice" in tags or ("quiet" in tags and solver_moves > 1)
    if not hard_enough or score < settings.top_min_score:
        return None
    return Scored(line, score, weak_found, tags)


def make_puzzle(meta: dict, game: chess.pgn.Game, cand: Candidate, sc: Scored) -> lichess.Puzzle:
    h = game.headers
    white, black = _surname(h.get("White", "White")), _surname(h.get("Black", "Black"))
    solver = white if chess.Board(cand.fen).turn == chess.WHITE else black
    event = meta.get("tour") or h.get("Event", "")
    year = (h.get("Date") or "")[:4]
    if year.isdigit() and year not in event:
        event = f"{event} {year}".strip()
    rnd = meta.get("round") or ""
    source = " · ".join(x for x in (f"{white} – {black}", event, rnd) if x)
    note = (f"In the game, {solver} found it! ✅" if cand.played == sc.line[0]
            else f"In the game, {solver} missed it 😮")
    url = meta.get("url") or next(
        (h[k] for k in ("ChapterURL", "GameURL", "Site") if h.get(k, "").startswith("http")),
        "https://lichess.org")
    pid = "top" + hashlib.sha1((cand.fen + " ".join(sc.line)).encode()).hexdigest()[:10]
    return lichess.Puzzle(id=pid, rating=0, themes=sc.tags, fen=cand.fen, last_move=cand.last_move,
                          solution=sc.line, game_url=url, source=source, note=note, url=url)


def game_key(meta: dict, game: chess.pgn.Game) -> str:
    if meta.get("key"):
        return meta["key"]
    h = game.headers
    return f"{meta.get('id')}|{h.get('White')}|{h.get('Black')}"


def find_best_puzzle(games: list[tuple[dict, chess.pgn.Game]], eng: Engine,
                     skip: set[str] = frozenset()) -> tuple[lichess.Puzzle, str] | None:
    budget = [settings.top_max_positions]
    cands = []
    for meta, g in games:
        if game_key(meta, g) in skip:
            continue
        cands += [(c, meta, g) for c in scan_game(g, eng, budget)]
    cands.sort(key=lambda x: x[0].priority, reverse=True)
    best = None
    for cand, meta, g in cands[: settings.top_max_deep]:
        sc = score_candidate(cand, eng)
        if sc:
            log.info("Candidate %s – %s [%s] score %d %s", g.headers.get("White"),
                     g.headers.get("Black"), cand.kind, sc.score, ",".join(sc.tags))
        if sc and (best is None or sc.score > best[0].score):
            best = (sc, cand, meta, g)
    if not best:
        return None
    sc, cand, meta, g = best
    return make_puzzle(meta, g, cand, sc), game_key(meta, g)


# --------------------------------------------------------------------------- state & refresh
_latest: lichess.Puzzle | None = None
_latest_at = 0.0
_used: list[str] = []          # games already turned into puzzles (don't repeat them)
_pushed = ""                   # id of the top puzzle last included in the daily post
_seen: list[str] = []          # games already scanned (puzzle or not) — skip them next time
_attempt_at = 0.0              # last time a search started (avoid restarting it on every tap)
_lock = asyncio.Lock()


def _state_file():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / "top_puzzle.json"


def load_saved() -> None:
    global _latest, _latest_at, _used, _pushed, _seen
    try:
        d = json.loads(_state_file().read_text("utf-8"))
        _seen = d.get("seen", [])
        _latest = lichess.pin(lichess.Puzzle(**d["puzzle"])) if d.get("puzzle") else None
        _latest_at, _used, _pushed = d.get("at", 0.0), d.get("used", []), d.get("pushed", "")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass


def _save() -> None:
    try:
        _state_file().write_text(json.dumps(
            {"puzzle": asdict(_latest) if _latest else None, "at": _latest_at,
             "used": _used[-200:], "pushed": _pushed, "seen": _seen[-3000:]},
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
    global _latest, _latest_at, _attempt_at
    if _lock.locked() or not settings.top_enabled:
        return None
    async with _lock:
        _attempt_at = time.time()
        path = find_stockfish()
        if not path:
            log.warning("Stockfish not found → top-game puzzles disabled "
                        "(Mac: brew install stockfish; Render: see scripts/get_stockfish.sh)")
            return None
        t0 = time.time()
        try:
            if settings.top_source == "broadcast":
                games = await fetch_recent_games()
            else:
                games = await fetch_master_games(set(_used) | set(_seen))
        except Exception:
            log.exception("Could not fetch games (%s)", settings.top_source)
            return None

        def work():
            with Engine(path) as eng:
                return find_best_puzzle(games, eng, set(_used))

        found = await asyncio.to_thread(work)
        _seen.extend(game_key(m, g) for m, g in games)
        if not found:
            _save()
            log.warning("No position hard enough today – keeping the previous puzzle (%.0fs)",
                        time.time() - t0)
            return None
        puzzle, key = found
        _latest, _latest_at = lichess.pin(puzzle), time.time()
        _used.append(key)
        _save()
        log.info("New top-game puzzle %s: %s (%.0fs)", puzzle.id, puzzle.source, time.time() - t0)
        return puzzle


_tasks: set = set()


def refresh_in_background() -> None:
    """Start a search unless one is running or one already ran in the last 6 hours."""
    if not _lock.locked() and time.time() - _attempt_at > 6 * 3600:
        t = asyncio.get_running_loop().create_task(refresh())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)

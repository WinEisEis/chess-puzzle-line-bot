"""Tiny client for the public Lichess puzzle API (no account needed)."""
from __future__ import annotations

import io
from dataclasses import dataclass

import chess
import chess.pgn
import httpx

from .config import settings

API = "https://lichess.org/api"


@dataclass
class Puzzle:
    id: str
    rating: int
    themes: list[str]
    fen: str                 # position the solver sees
    last_move: str | None    # opponent's move that led to it (UCI), for highlighting
    solution: list[str]      # UCI moves, solver first, then alternating replies
    game_url: str
    # Only for puzzles taken from recent top games (see topgames.py):
    source: str = ""         # e.g. "Gukesh D – Carlsen, Magnus · Norway Chess 2026 · Round 5"
    note: str = ""           # e.g. "In the game, Gukesh found it!"
    url: str = ""            # link to the real game (shown only after solving)

    @property
    def turn(self) -> chess.Color:
        return chess.Board(self.fen).turn

    @property
    def side_to_move(self) -> str:
        return "White" if self.turn == chess.WHITE else "Black"

    @property
    def lichess_url(self) -> str:
        return self.url or f"https://lichess.org/training/{self.id}"

    @property
    def is_top_game(self) -> bool:
        return bool(self.source)

    def solution_san(self) -> str:
        """e.g. '1. Qxf7#'  or  '23... Rxe1+ 24. Kh2 Qh4#'"""
        board = chess.Board(self.fen)
        return board.variation_san([chess.Move.from_uci(m) for m in self.solution])

    def solution_steps(self) -> list[tuple[str, bool]]:
        """[(san, is_solver_move), ...] — used to bold the moves you must find."""
        board = chess.Board(self.fen)
        out = []
        for i, uci in enumerate(self.solution):
            mv = chess.Move.from_uci(uci)
            out.append((board.san(mv), i % 2 == 0))
            board.push(mv)
        return out



def check_line(p: "Puzzle", line: list[str]) -> dict:
    """Check moves played on the web board against the solution.

    `line` = every move so far (player moves + the replies the server sent back), in UCI.
    Returns {"correct": bool, "reply": uci|None, "done": bool}. The solution itself is never
    sent to the browser, so nobody can peek at it in the page source.
    Like Lichess, any move that gives checkmate is accepted even if it's not the listed one.
    """
    board = chess.Board(p.fen)
    sol = p.solution
    for i, uci in enumerate(line):
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            return {"correct": False, "reply": None, "done": False}
        if i >= len(sol) or mv not in board.legal_moves:
            return {"correct": False, "reply": None, "done": False}
        expected = sol[i]
        if uci != expected and i % 2 == 0 and uci[:4] == expected[:4]:
            uci, mv = expected, chess.Move.from_uci(expected)  # same move, other promotion piece
        if uci != expected:
            if i % 2 == 0:  # player's move: alternative mate is fine
                board.push(mv)
                ok = board.is_checkmate()
                return {"correct": ok, "reply": None, "done": ok}
            return {"correct": False, "reply": None, "done": False}  # tampered reply
        board.push(mv)
    n = len(line)
    if n >= len(sol):
        return {"correct": True, "reply": None, "done": True}
    reply = sol[n] if n % 2 == 1 else None
    done = n + 1 >= len(sol) if reply else False
    return {"correct": True, "reply": reply, "done": done}


def hint_square(p: "Puzzle", ply: int) -> str | None:
    """Square of the piece the player should move next."""
    if 0 <= ply < len(p.solution) and ply % 2 == 0:
        return p.solution[ply][:2]
    return None


def _replay(pgn: str, plies: int | None) -> tuple[chess.Board, str | None]:
    game = chess.pgn.read_game(io.StringIO(pgn))
    board = game.board() if game else chess.Board()
    last = None
    for i, mv in enumerate(game.mainline_moves() if game else []):
        if plies is not None and i >= plies:
            break
        board.push(mv)
        last = mv.uci()
    return board, last


def parse_puzzle(data: dict) -> Puzzle:
    """Turn the Lichess JSON ({"game": {...}, "puzzle": {...}}) into a Puzzle.

    Lichess cuts the game PGN at the puzzle start, so replaying the whole PGN gives the
    position to solve. As a safety net we also try `initialPly` / `initialPly + 1` and keep
    whichever position makes the first solution move legal.
    """
    g, p = data["game"], data["puzzle"]
    solution = p["solution"]
    first = chess.Move.from_uci(solution[0])
    init = p.get("initialPly")

    candidates: list[int | None] = [None]
    if init is not None:
        candidates += [init + 1, init]

    board, last = None, None
    for plies in candidates:
        b, l = _replay(g["pgn"], plies)
        if first in b.legal_moves:
            board, last = b, l
            break
    if board is None:
        raise ValueError(f"Could not rebuild position for puzzle {p.get('id')}")

    return Puzzle(
        id=p["id"],
        rating=int(p.get("rating", 0)),
        themes=list(p.get("themes", [])),
        fen=board.fen(),
        last_move=last,
        solution=solution,
        game_url=f"https://lichess.org/{g.get('id', '')}",
    )


# Small in-memory cache so the image endpoint and the "solution" button don't refetch.
_cache: dict[str, Puzzle] = {}
_pinned: dict[str, Puzzle] = {}   # top-game puzzles: never evicted, never fetched from Lichess


def is_top_id(puzzle_id: str) -> bool:
    """Our own ids look like 'top3f9a1c2b7e' — Lichess puzzle ids are always 5 characters."""
    return puzzle_id.startswith("top") and len(puzzle_id) > 5


def pin(p: Puzzle) -> Puzzle:
    _pinned[p.id] = p
    return p


def remember(p: Puzzle) -> Puzzle:
    _cache[p.id] = p
    if len(_cache) > 500:  # keep it bounded
        _cache.pop(next(iter(_cache)))
    return p


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": "chess-puzzle-line-bot/1.0"}
    if settings.lichess_token:
        h["Authorization"] = f"Bearer {settings.lichess_token}"
    return h


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15, headers=_headers()) as client:
        r = await client.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def daily() -> Puzzle:
    return remember(parse_puzzle(await _get("/puzzle/daily")))


async def by_id(puzzle_id: str) -> Puzzle:
    if puzzle_id in _pinned:
        return _pinned[puzzle_id]
    if is_top_id(puzzle_id):
        raise KeyError(f"Top-game puzzle {puzzle_id} is no longer available")
    if puzzle_id in _cache:
        return _cache[puzzle_id]
    return remember(parse_puzzle(await _get(f"/puzzle/{puzzle_id}")))


async def next_by_theme(theme: str) -> Puzzle:
    params = {"angle": theme}
    if settings.difficulty:
        params["difficulty"] = settings.difficulty
    return remember(parse_puzzle(await _get("/puzzle/next", params)))

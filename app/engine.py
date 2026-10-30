"""Find and start Stockfish (free chess engine).

Where it looks, in order:
  1. STOCKFISH_PATH in .env
  2. bin/stockfish inside this project (Render downloads it there — see scripts/get_stockfish.sh)
  3. `stockfish` on your PATH (Mac: brew install stockfish)
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parent.parent


def find_stockfish() -> str | None:
    candidates = [os.getenv("STOCKFISH_PATH", ""), str(ROOT / "bin" / "stockfish"),
                  shutil.which("stockfish") or ""]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


class Engine:
    """Small synchronous wrapper. Run it in a thread (asyncio.to_thread) from async code."""

    def __init__(self, path: str, hash_mb: int = 16) -> None:
        self._e = chess.engine.SimpleEngine.popen_uci(path)
        self._e.configure({"Threads": 1, "Hash": hash_mb})

    def close(self) -> None:
        try:
            self._e.quit()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def top_moves(self, board: chess.Board, nodes: int, multipv: int = 2) -> list[tuple[chess.Move, int]]:
        """Best moves with scores in centipawns from the side to move's view (mate ≈ ±10000)."""
        infos = self._e.analyse(board, chess.engine.Limit(nodes=nodes), multipv=multipv)
        if isinstance(infos, dict):
            infos = [infos]
        out = []
        for info in infos:
            if "pv" not in info or not info["pv"]:
                continue
            score = info["score"].pov(board.turn).score(mate_score=10000)
            out.append((info["pv"][0], score))
        return out

"""Draw a chess position to PNG with Pillow only (no Cairo, works on Windows/macOS/Linux).

Looks like the default Lichess board: brown squares + the "cburnett" piece set.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import chess
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent / "assets"
PIECES_DIR = ASSETS / "pieces"
FONT_BOLD = ASSETS / "DejaVuSans-Bold.ttf"

# Lichess "brown" board
LIGHT = (240, 217, 181)
DARK = (181, 136, 99)
HL_LIGHT = (205, 210, 106)   # last-move highlight
HL_DARK = (170, 162, 58)


@lru_cache(maxsize=32)
def _piece(code: str, size: int) -> Image.Image:
    """code like 'wK' or 'bN' → RGBA image scaled to `size` px."""
    img = Image.open(PIECES_DIR / f"{code}.png").convert("RGBA")
    return img.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=256)
def render_png(fen: str, last_move: str | None = None, flip: bool | None = None,
               size: int = 800) -> bytes:
    """Return PNG bytes. By default the board is shown from the side to move."""
    board = chess.Board(fen)
    if flip is None:
        flip = board.turn == chess.BLACK

    sq = size // 8
    size = sq * 8
    img = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(FONT_BOLD), max(10, sq // 6))
    pad = max(2, sq // 20)

    hl: set[int] = set()
    if last_move:
        mv = chess.Move.from_uci(last_move)
        hl = {mv.from_square, mv.to_square}

    for rank in range(8):
        for file in range(8):
            square = chess.square(file, rank)
            col = 7 - file if flip else file
            row = rank if flip else 7 - rank
            x0, y0 = col * sq, row * sq
            light = (file + rank) % 2 == 1
            color = (HL_LIGHT if light else HL_DARK) if square in hl else (LIGHT if light else DARK)
            d.rectangle([x0, y0, x0 + sq - 1, y0 + sq - 1], fill=color)

            # Coordinates inside the board like Lichess: ranks on the left, files on the bottom.
            label = DARK if light else LIGHT
            if col == 0:
                d.text((x0 + pad, y0 + pad), str(rank + 1), font=font, fill=label, anchor="la")
            if row == 7:
                d.text((x0 + sq - pad, y0 + sq - pad), "abcdefgh"[file], font=font,
                       fill=label, anchor="rd")

            piece = board.piece_at(square)
            if piece:
                code = ("w" if piece.color == chess.WHITE else "b") + piece.symbol().upper()
                img.alpha_composite(_piece(code, sq), (x0, y0))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()

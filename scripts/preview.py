"""Try the puzzle part WITHOUT LINE: fetch a puzzle, save the board PNG, print the solution.

    python scripts/preview.py            # today's daily puzzle
    python scripts/preview.py endgame    # any Lichess theme: opening, middlegame, mateIn2, fork...
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cards, lichess  # noqa: E402
from app.board import render_png  # noqa: E402


async def main(theme: str | None) -> None:
    p = await (lichess.next_by_theme(theme) if theme else lichess.daily())
    out = Path(f"preview_{p.id}.png")
    out.write_bytes(render_png(p.fen, p.last_move))

    print(f"Puzzle   : {p.id}  (rating {p.rating})  {p.lichess_url}")
    print(f"Themes   : {', '.join(p.themes)}")
    print(f"To move  : {p.side_to_move}")
    print(f"FEN      : {p.fen}")
    print(f"Solution : {p.solution_san()}")
    print(f"Image    : {out.resolve()}")

    card = Path(f"preview_{p.id}_card.json")
    card.write_text(json.dumps(cards.puzzle_bubble(p, "Preview"), indent=2, ensure_ascii=False), "utf-8")
    print(f"Flex JSON: {card.resolve()}  (paste into https://developers.line.biz/flex-simulator/)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))

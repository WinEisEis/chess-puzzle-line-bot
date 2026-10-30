"""LINE Flex Message layouts (plain dicts → easy to tweak).

Preview/edit any card visually by pasting the dict as JSON into the
LINE Flex Message Simulator: https://developers.line.biz/flex-simulator/
"""
from __future__ import annotations

from urllib.parse import urlencode

from .config import settings
from .lichess import Puzzle

GREEN = "#629924"   # Lichess-ish green
DARK = "#312E2B"
MUTED = "#8C8C8C"


def _postback(label: str, data: dict, display: str | None = None, style: str = "secondary",
              color: str | None = None) -> dict:
    action = {"type": "postback", "label": label, "data": urlencode(data)}
    if display:
        action["displayText"] = display  # shown in the chat as if the user typed it
    btn = {"type": "button", "style": style, "height": "sm", "action": action}
    if color:
        btn["color"] = color
    return btn


def category_buttons() -> dict:
    """One full-width button per row for each category (Opening / Middlegame / Endgame)."""
    buttons = [
        _postback(label[:20], {"a": "new", "t": theme}, display=f"{label} puzzle please!")
        for theme, label in settings.categories.items()
    ]
    buttons.append(_postback("🏆 Top game", {"a": "top"}, display="🏆 Top game puzzle please!"))
    return {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons}


def puzzle_bubble(p: Puzzle, title: str) -> dict:
    img = f"{settings.base_url}/board/{p.id}.png"
    play = f"{settings.base_url}/play/{p.id}"
    nice_themes = ", ".join(t for t in p.themes[:4])
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": DARK, "paddingAll": "md",
            "contents": [
                {"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "color": "#DDDDDD", "size": "sm",
                 "text": (f"{p.side_to_move} to move · from a real game" if p.is_top_game
                          else f"{p.side_to_move} to move · Rating {p.rating}")},
            ],
        },
        "hero": {
            "type": "image", "url": img, "size": "full", "aspectRatio": "1:1", "aspectMode": "cover",
            # Tap the board → open the interactive board
            "action": {"type": "uri", "uri": play},
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "md",
            "contents": [
                {"type": "text", "text": f"Find the best move for {p.side_to_move}!",
                 "weight": "bold", "wrap": True},
                {"type": "text", "size": "xs", "color": MUTED, "wrap": True,
                 "text": (f"🏆 {p.source}" if p.is_top_game
                          else f"Themes: {nice_themes}" if nice_themes else " ")},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": GREEN,
                 "action": {"type": "uri", "label": "♟ Play on board", "uri": play}},
                _postback("💡 Show solution", {"a": "sol", "id": p.id}, display="💡 Show solution"),
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "Want another? Pick a type:", "size": "xs",
                 "color": MUTED, "margin": "md"},
                category_buttons(),
            ],
        },
    }


def solution_bubble(p: Puzzle) -> dict:
    lines = []
    for i, (san, mine) in enumerate(p.solution_steps()):
        lines.append({
            "type": "text", "size": "md", "wrap": True,
            "text": f"{'✅' if mine else '↪'}  {san}",
            "weight": "bold" if mine else "regular",
            "color": DARK if mine else MUTED,
        })
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "💡 Solution", "weight": "bold", "size": "lg"},
                {"type": "text", "text": p.solution_san(), "size": "sm", "color": GREEN, "wrap": True},
                {"type": "separator", "margin": "md"},
                *lines,
                *([{"type": "text", "text": p.note, "size": "sm", "wrap": True, "margin": "md"}]
                  if p.note else []),
                {"type": "text", "text": "✅ = your move   ↪ = opponent's reply",
                 "size": "xxs", "color": MUTED, "margin": "md", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": ("See the game ↗" if p.is_top_game
                                                     else "Replay on Lichess ↗"),
                            "uri": p.lichess_url}},
                category_buttons(),
            ],
        },
    }


def menu_bubble() -> dict:
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "♞ Chess Puzzle Bot", "weight": "bold", "size": "lg"},
                {"type": "text", "wrap": True, "size": "sm", "color": MUTED,
                 "text": "I post a puzzle every day. Tap a type to get one now:"},
                category_buttons(),
                _postback("📅 Today's puzzle", {"a": "daily"},
                          display="📅 Today's daily puzzle", style="primary", color=GREEN),
            ],
        },
    }

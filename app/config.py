"""All settings come from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _difficulty(value: str) -> str:
    v = value.strip().lower() or "harder"
    return {"hard": "harder", "easy": "easier", "medium": "normal"}.get(v, v)


# Buttons shown on every card. key = Lichess theme ("angle"), value = button label
# (keep labels short: three buttons share one row on a phone screen).
# Any Lichess theme works here, e.g. "mateIn2", "fork", "pin", "sacrifice", "short".
# Full list: https://lichess.org/training/themes
DEFAULT_CATEGORIES: dict[str, str] = {
    "opening": "Opening",
    "middlegame": "Middlegame",
    "endgame": "Endgame",
}


@dataclass(frozen=True)
class Settings:
    channel_secret: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_SECRET", ""))
    channel_access_token: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
    # Alternative to the long-lived token: Channel ID (+ the secret above). The bot then
    # issues its own 15-minute token and renews it automatically.
    channel_id: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_ID", ""))

    # Public HTTPS address of this server (ngrok / cloudflared / your host). LINE downloads
    # the board images from BASE_URL/board/<id>.png, so it must be reachable from the internet.
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"))

    # The bot only reacts to messages that start with one of these (comma separated),
    # e.g. "/chess endgame". Normal chat is ignored. Buttons always work.
    command_prefixes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip().lower() for x in os.getenv("COMMAND_PREFIXES", "/chess,/หมากรุก").split(",") if x.strip()
        )
    )

    # "Think first": the Show-solution button only works N minutes after the puzzle was posted.
    solution_lock_minutes: int = field(default_factory=lambda: int(os.getenv("SOLUTION_LOCK_MINUTES", "10")))
    # Reply when someone asks too early. {minutes} = lock length, {left} = minutes remaining.
    lock_message: str = field(default_factory=lambda: os.getenv(
        "LOCK_MESSAGE",
        "🤔 คิดก่อนดิ! ให้เวลา {minutes} นาทีถึงจะเฉลย\n⏳ เหลืออีก {left} นาที"))

    # Daily push
    daily_push_enabled: bool = field(default_factory=lambda: _bool("DAILY_PUSH_ENABLED", True))
    daily_push_time: str = field(default_factory=lambda: os.getenv("DAILY_PUSH_TIME", "08:00"))  # HH:MM
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Bangkok"))

    # Protects POST /admin/push-daily (used for manual tests or an external cron).
    admin_key: str = field(default_factory=lambda: os.getenv("ADMIN_KEY", ""))

    # Extra group IDs to always push to (comma separated). Useful on hosts with no
    # persistent disk, where data/groups.json is wiped on restart.
    extra_group_ids: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            g.strip() for g in os.getenv("GROUP_IDS", "").split(",") if g.strip()
        )
    )

    # Optional Lichess personal token (no scopes needed). Raises rate limits a bit.
    lichess_token: str = field(default_factory=lambda: os.getenv("LICHESS_TOKEN", ""))

    # Difficulty for category puzzles (Opening / Middlegame / Endgame buttons):
    # easiest | easier | normal | harder | hardest   ("hard" is accepted as "harder")
    # Default: harder. The Lichess *daily* puzzle is chosen by Lichess and isn't affected.
    difficulty: str = field(default_factory=lambda: _difficulty(os.getenv("PUZZLE_DIFFICULTY", "")))

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT / "data"))))

    categories: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CATEGORIES))


settings = Settings()

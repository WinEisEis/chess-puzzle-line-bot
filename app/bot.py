"""What the bot does when something happens in LINE."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import parse_qs

import certifi  # trusted CA list, so HTTPS works even if macOS Python has no certificates
import httpx
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    FlexContainer,
    FlexMessage,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FollowEvent,
    GroupSource,
    JoinEvent,
    LeaveEvent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from . import cards, lichess, store, topgames
from .config import settings

log = logging.getLogger("bot")

HELP_WORDS = {"", "menu", "help", "เมนู", "ช่วย"}
DAILY_WORDS = {"daily", "today", "วันนี้"}
THAI_ALIASES = {"เปิดเกม": "opening", "กลางเกม": "middlegame", "ท้ายเกม": "endgame", "จบเกม": "endgame",
                "เกมดัง": "top", "ท็อป": "top"}
TOP_WORDS = {"top", "pro", "gm"}


def parse_command(text: str) -> str | None:
    """Return what comes after the command prefix, or None if the message isn't for the bot.

    "/chess endgame" → "endgame",  "/chess" → "",  "endgame" → None (ignored)
    """
    t = text.strip().lower()
    for prefix in settings.command_prefixes:
        if t == prefix or t.startswith(prefix + " "):
            arg = t[len(prefix):].strip()
            return THAI_ALIASES.get(arg, arg)
    return None


# --------------------------------------------------------------------------- LINE I/O
TOKEN_URL = "https://api.line.me/oauth2/v3/token"


async def issue_stateless_token(channel_id: str, channel_secret: str) -> tuple[str, int]:
    """Get a short-lived (15 min) "stateless" access token from Channel ID + Channel secret.
    No 'Issue' button needed. Returns (token, seconds_valid)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": channel_id,
            "client_secret": channel_secret,
        })
    if r.status_code != 200:
        raise RuntimeError(f"LINE token request failed {r.status_code}: {r.text}")
    d = r.json()
    return d["access_token"], int(d.get("expires_in", 900))


class Line:
    """Thin wrapper around the LINE SDK.

    Auth, in order of preference:
      1. LINE_CHANNEL_ACCESS_TOKEN (long-lived token), or
      2. LINE_CHANNEL_ID + LINE_CHANNEL_SECRET → the bot issues a 15-minute token itself
         and renews it automatically before it expires.
    With neither, it only logs messages (dry-run mode).
    """

    def __init__(self) -> None:
        self._client: AsyncApiClient | None = None
        self._config: Configuration | None = None
        self._expires_at = 0.0  # 0 = long-lived token, never refresh
        self._lock = asyncio.Lock()
        self.api: AsyncMessagingApi | None = None
        self.sent: list[tuple[str, str, list]] = []  # kept for tests / debugging

    async def start(self) -> None:
        if settings.channel_access_token:
            self._config = Configuration(access_token=settings.channel_access_token, ssl_ca_cert=certifi.where())
            log.info("LINE auth: long-lived channel access token")
        elif settings.channel_id and settings.channel_secret:
            self._config = Configuration(access_token="", ssl_ca_cert=certifi.where())
            self._expires_at = 1.0  # force a fetch on first use
            log.info("LINE auth: Channel ID + secret (auto-renewing 15-minute token)")
        else:
            log.warning("No LINE token and no LINE_CHANNEL_ID → DRY-RUN mode (messages are only logged)")
            return
        self._client = AsyncApiClient(self._config)
        self.api = AsyncMessagingApi(self._client)

    async def _ensure_token(self) -> None:
        if not self._expires_at or time.time() < self._expires_at:
            return
        async with self._lock:
            if time.time() < self._expires_at:
                return  # another request already renewed it
            token, ttl = await issue_stateless_token(settings.channel_id, settings.channel_secret)
            self._config.access_token = token
            self._expires_at = time.time() + ttl - 60  # renew a minute early
            log.info("Issued new LINE stateless token (valid %d min)", ttl // 60)

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def reply(self, token: str, messages: list) -> None:
        """Replies are FREE and don't count toward your monthly quota."""
        self.sent.append(("reply", token, messages))
        if self.api:
            await self._ensure_token()
            await self.api.reply_message(ReplyMessageRequest(reply_token=token, messages=messages))
        else:
            log.info("DRY reply → %s", [m.alt_text if hasattr(m, "alt_text") else m.text for m in messages])

    async def push(self, to: str, messages: list) -> None:
        """Push messages DO count: 1 message × number of people in the group."""
        self.sent.append(("push", to, messages))
        if self.api:
            await self._ensure_token()
            await self.api.push_message(PushMessageRequest(to=to, messages=messages))
        else:
            log.info("DRY push %s → %s", to, [getattr(m, "alt_text", None) for m in messages])


line = Line()


def flex(alt: str, bubble: dict) -> FlexMessage:
    return FlexMessage(alt_text=alt[:400], contents=FlexContainer.from_dict(bubble))


def puzzle_message(p: lichess.Puzzle, title: str) -> FlexMessage:
    return flex(f"{title}: {p.side_to_move} to move", cards.puzzle_bubble(p, title))


def solution_message(p: lichess.Puzzle) -> FlexMessage:
    return flex(f"Solution: {p.solution_san()}", cards.solution_bubble(p))


def help_text() -> str:
    p = settings.command_prefixes[0]
    cats = " / ".join(settings.categories)
    return ("♟ Chess Puzzle Bot\n"
            f"I post a puzzle here every day at {settings.daily_push_time}.\n\n"
            f"Commands:\n"
            f"{p} → menu\n"
            f"{p} daily → today's puzzle\n"
            f"{p} <type> → {cats}\n"
            f"{p} top → position from the latest top games\n"
            f"(e.g. {p} endgame)")


def menu_message() -> FlexMessage:
    return flex("Chess Puzzle Bot – pick a puzzle type", cards.menu_bubble())


# --------------------------------------------------------------------------- actions
async def make_category_puzzle(theme: str) -> tuple[FlexMessage, str]:
    p = await lichess.next_by_theme(theme)
    label = settings.categories.get(theme, theme.title())
    return puzzle_message(p, f"♞ {label} Puzzle"), p.id


async def make_daily_puzzle() -> tuple[FlexMessage, str]:
    p = await lichess.daily()
    return puzzle_message(p, "📅 Daily Puzzle"), p.id


TOP_TITLE = "🏆 Top Game Puzzle"
TOP_WAIT = ("⏳ Stockfish กำลังคุ้ยเกม classical ของ GM ระดับท็อปหาโจทย์ยาก ๆ อยู่ "
            "(รอบแรกอาจใช้เวลาเกือบชั่วโมง) ลองใหม่อีกทีทีหลังนะ")


async def make_top_puzzle() -> tuple[FlexMessage, str]:
    p = topgames.latest()
    if not topgames.is_fresh():
        topgames.refresh_in_background()   # get a newer one ready for next time
    if p is None:
        raise LookupError("no top-game puzzle yet")
    return puzzle_message(p, TOP_TITLE), p.id


# ---- "think first" lock: no solution until the puzzle has been out for N minutes ----
_shown_at: dict[tuple[str, str], float] = {}   # (chat id, puzzle id) → time first posted


def mark_shown(chat: str | None, puzzle_id: str) -> None:
    if chat:
        _shown_at.setdefault((chat, puzzle_id), time.time())
        if len(_shown_at) > 2000:              # keep memory bounded
            _shown_at.pop(next(iter(_shown_at)))


def seconds_until_solution(chat: str | None, puzzle_id: str) -> int:
    """0 = solution may be shown. Unknown puzzles (e.g. after a server restart) are not locked."""
    t = _shown_at.get((chat, puzzle_id)) if chat else None
    if t is None:
        return 0
    return max(0, int(t + settings.solution_lock_minutes * 60 - time.time()))


def lock_message(seconds_left: int) -> TextMessage:
    mins = max(1, -(-seconds_left // 60))   # round up
    return TextMessage(text=settings.lock_message.format(
        minutes=settings.solution_lock_minutes, left=mins))


async def push_daily_to_all() -> dict:
    """Called by the scheduler (and POST /admin/push-daily)."""
    groups = store.all_groups()
    if not groups:
        log.warning("No groups registered yet – invite the bot to a group first.")
        return {"sent": 0, "groups": []}
    msg, pid = await make_daily_puzzle()
    messages, ids = [msg], [pid]
    # Add the top-game puzzle to the SAME push: still counts as 1 message per person.
    top = topgames.latest()
    if top and top.id != topgames.last_pushed():
        messages.append(puzzle_message(top, TOP_TITLE))
        ids.append(top.id)
    ok = []
    for gid in groups:
        try:
            await line.push(gid, messages)
            for i in ids:
                mark_shown(gid, i)
            ok.append(gid)
        except Exception:  # one bad group shouldn't stop the others
            log.exception("Push to %s failed", gid)
    if len(ids) > 1 and ok:
        topgames.mark_pushed(ids[1])
    if not topgames.is_fresh():
        topgames.refresh_in_background()
    return {"sent": len(ok), "groups": ok, "puzzles": ids}


# --------------------------------------------------------------------------- events
def _group_id(event) -> str | None:
    src = getattr(event, "source", None)
    return src.group_id if isinstance(src, GroupSource) else None


def _chat_id(event) -> str | None:
    """Group, multi-person room, or 1:1 user — whatever chat the event came from."""
    src = getattr(event, "source", None)
    return (getattr(src, "group_id", None) or getattr(src, "room_id", None)
            or getattr(src, "user_id", None))


async def _reply_puzzle(event, build) -> None:
    """Fetch a puzzle, reply with its card, and start its solution timer for this chat."""
    try:
        msg, pid = await build()
    except LookupError:   # top-game puzzle still being analysed
        await line.reply(event.reply_token, [TextMessage(text=TOP_WAIT)])
        return
    except Exception:
        log.exception("Failed to fetch puzzle")
        await line.reply(event.reply_token,
                         [TextMessage(text="😵 Lichess didn't answer. Please try again in a moment.")])
        return
    await line.reply(event.reply_token, [msg])
    mark_shown(_chat_id(event), pid)


async def _safe_reply(token: str, build) -> None:
    try:
        msgs = await build()
    except Exception:
        log.exception("Failed to build reply")
        msgs = [TextMessage(text="😵 Lichess didn't answer. Please try again in a moment.")]
    await line.reply(token, msgs)


async def handle_event(event) -> None:
    gid = _group_id(event)
    if gid and not isinstance(event, LeaveEvent):
        store.add_group(gid)  # also catches groups the bot joined before the server was up

    if isinstance(event, JoinEvent):
        log.info("Joined group %s", gid)
        await line.reply(event.reply_token, [
            TextMessage(text=help_text()),
            menu_message(),
        ])

    elif isinstance(event, LeaveEvent):
        if gid:
            store.remove_group(gid)
            log.info("Removed from group %s", gid)

    elif isinstance(event, FollowEvent):
        await line.reply(event.reply_token, [menu_message()])

    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        cmd = parse_command(event.message.text)
        if cmd is None:
            return  # normal chat (no "/chess" prefix) → stay quiet
        if cmd in settings.categories:
            await _reply_puzzle(event, lambda: make_category_puzzle(cmd))
        elif cmd in DAILY_WORDS:
            await _reply_puzzle(event, make_daily_puzzle)
        elif cmd in TOP_WORDS:
            await _reply_puzzle(event, make_top_puzzle)
        elif cmd in HELP_WORDS:
            await line.reply(event.reply_token, [menu_message()])
        else:  # "/chess something-unknown" → show how to use it
            await line.reply(event.reply_token, [TextMessage(text=help_text()), menu_message()])

    elif isinstance(event, PostbackEvent):
        data = {k: v[0] for k, v in parse_qs(event.postback.data).items()}
        action = data.get("a")
        if action == "sol" and data.get("id"):
            left = seconds_until_solution(_chat_id(event), data["id"])
            if left > 0:
                await line.reply(event.reply_token, [lock_message(left)])
            else:
                await _safe_reply(event.reply_token,
                                  lambda: _one_sync(lichess.by_id(data["id"]), solution_message))
        elif action == "new" and data.get("t"):
            await _reply_puzzle(event, lambda: make_category_puzzle(data["t"]))
        elif action == "daily":
            await _reply_puzzle(event, make_daily_puzzle)
        elif action == "top":
            await _reply_puzzle(event, make_top_puzzle)


async def _one(coro) -> list:
    return [await coro]


async def _one_sync(coro, fn) -> list:
    return [fn(await coro)]

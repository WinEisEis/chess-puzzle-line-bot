"""FastAPI server: LINE webhook + board images + daily scheduler.

Run:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError

from . import lichess, store, topgames
from .board import render_png
from .bot import handle_event, line, push_daily_to_all
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

parser = WebhookParser(settings.channel_secret or "dry-run-secret")
scheduler = AsyncIOScheduler(timezone=settings.timezone)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await line.start()
    if settings.daily_push_enabled:
        hh, mm = settings.daily_push_time.split(":")
        scheduler.add_job(push_daily_to_all, CronTrigger(hour=int(hh), minute=int(mm)),
                          id="daily", replace_existing=True, misfire_grace_time=3600)
        log.info("Daily puzzle scheduled at %s (%s)", settings.daily_push_time, settings.timezone)
    if settings.top_enabled:
        topgames.load_saved()
        hh, mm = settings.top_refresh_time.split(":")
        scheduler.add_job(topgames.refresh, CronTrigger(hour=int(hh), minute=int(mm)),
                          id="top", replace_existing=True, misfire_grace_time=3600)
        log.info("Top-game puzzle refresh scheduled at %s (%s)", settings.top_refresh_time,
                 settings.timezone)
        if not topgames.is_fresh():
            topgames.refresh_in_background()   # e.g. right after a Render restart
    if scheduler.get_jobs():
        scheduler.start()
    log.info("Board images will be served from %s/board/<id>.png", settings.base_url)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await line.close()


app = FastAPI(title="Chess Puzzle LINE Bot", lifespan=lifespan)

APP_DIR = Path(__file__).parent
app.mount("/static/pieces", StaticFiles(directory=APP_DIR / "assets" / "pieces"), name="pieces")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
PLAY_HTML = (APP_DIR / "static" / "play.html").read_text("utf-8")


@app.get("/")
async def health():
    return {"ok": True, "groups": len(store.all_groups()), "base_url": settings.base_url}


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(default="")):
    body = (await request.body()).decode("utf-8")
    try:
        events = parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    for event in events:
        try:
            await handle_event(event)
        except Exception:
            log.exception("Error handling %s", type(event).__name__)
    return "OK"


@app.get("/board/{puzzle_id}.png")
async def board_image(puzzle_id: str):
    try:
        p = await lichess.by_id(puzzle_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Unknown puzzle")
    png = render_png(p.fen, p.last_move)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ------------------------------------------------------------------ interactive board page
async def _puzzle_or_404(puzzle_id: str):
    try:
        return await lichess.by_id(puzzle_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Unknown puzzle")


@app.get("/play/{puzzle_id}", response_class=HTMLResponse)
async def play_page(puzzle_id: str):
    """Opens inside LINE's in-app browser when someone taps "Play on board"."""
    if not puzzle_id.isalnum():
        raise HTTPException(status_code=404)
    return PLAY_HTML.replace("__PUZZLE_ID__", puzzle_id)


@app.get("/api/puzzle/{puzzle_id}")
async def api_puzzle(puzzle_id: str):
    p = await _puzzle_or_404(puzzle_id)
    # Note: no solution here — moves are checked on the server.
    return {"id": p.id, "fen": p.fen, "last_move": p.last_move, "rating": p.rating,
            "themes": p.themes, "lichess_url": p.lichess_url, "source": p.source}


class CheckBody(BaseModel):
    line: list[str]


@app.post("/api/puzzle/{puzzle_id}/check")
async def api_check(puzzle_id: str, body: CheckBody):
    p = await _puzzle_or_404(puzzle_id)
    return lichess.check_line(p, body.line[:40])


@app.get("/api/puzzle/{puzzle_id}/hint")
async def api_hint(puzzle_id: str, ply: int = 0):
    p = await _puzzle_or_404(puzzle_id)
    return {"square": lichess.hint_square(p, ply)}


@app.post("/admin/push-daily")
async def admin_push_daily(x_admin_key: str = Header(default="")):
    """Send today's puzzle to every group right now (for testing or an external cron)."""
    if not settings.admin_key or x_admin_key != settings.admin_key:
        raise HTTPException(status_code=403, detail="Bad or missing X-Admin-Key")
    return await push_daily_to_all()


@app.post("/admin/refresh-top")
async def admin_refresh_top(x_admin_key: str = Header(default="")):
    """Start looking for a new top-game puzzle now (runs in the background, a few minutes)."""
    if not settings.admin_key or x_admin_key != settings.admin_key:
        raise HTTPException(status_code=403, detail="Bad or missing X-Admin-Key")
    started = not topgames.is_running()
    topgames.refresh_in_background()
    cur = topgames.latest()
    return {"started": started, "current": cur.source if cur else None}


@app.get("/admin/groups")
async def admin_groups(x_admin_key: str = Header(default="")):
    if not settings.admin_key or x_admin_key != settings.admin_key:
        raise HTTPException(status_code=403, detail="Bad or missing X-Admin-Key")
    return {"groups": store.all_groups()}

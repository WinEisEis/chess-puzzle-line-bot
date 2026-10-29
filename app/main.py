"""FastAPI server: LINE webhook + board images + daily scheduler.

Run:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Request, Response
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError

from . import lichess, store
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
        scheduler.start()
        log.info("Daily puzzle scheduled at %s (%s)", settings.daily_push_time, settings.timezone)
    log.info("Board images will be served from %s/board/<id>.png", settings.base_url)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await line.close()


app = FastAPI(title="Chess Puzzle LINE Bot", lifespan=lifespan)


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


@app.post("/admin/push-daily")
async def admin_push_daily(x_admin_key: str = Header(default="")):
    """Send today's puzzle to every group right now (for testing or an external cron)."""
    if not settings.admin_key or x_admin_key != settings.admin_key:
        raise HTTPException(status_code=403, detail="Bad or missing X-Admin-Key")
    return await push_daily_to_all()


@app.get("/admin/groups")
async def admin_groups(x_admin_key: str = Header(default="")):
    if not settings.admin_key or x_admin_key != settings.admin_key:
        raise HTTPException(status_code=403, detail="Bad or missing X-Admin-Key")
    return {"groups": store.all_groups()}

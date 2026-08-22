import asyncio
from contextlib import suppress

from ..db import SessionLocal
from . import docker_ws


async def idle_reaper(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            await asyncio.to_thread(docker_ws.reap_idle, db)
        except Exception:
            pass
        finally:
            db.close()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=30)

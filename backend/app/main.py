import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db
from .routers.agent import router as agent_router
from .routers.workspaces import router as workspaces_router
from .services import docker_ws
from .services.reaper import idle_reaper


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop_event = asyncio.Event()

    def boot() -> None:
        try:
            docker_ws.ensure_network()
            docker_ws.ensure_image()
        except Exception:
            pass

    boot_task = asyncio.create_task(asyncio.to_thread(boot))
    reaper_task = asyncio.create_task(idle_reaper(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        reaper_task.cancel()
        boot_task.cancel()


app = FastAPI(title="Tomato Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(workspaces_router)
app.include_router(agent_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

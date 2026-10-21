from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from . import jobs
from .config import get_settings
from .hitl import router as hitl_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="Orquestador de compras multi-agente", lifespan=lifespan)
app.include_router(hitl_router)


class NuevaTarea(BaseModel):
    consulta: str = Field(min_length=5, max_length=2000)
    thread_id: str | None = Field(default=None, max_length=100)


@app.post("/tasks", status_code=202)
async def crear_tarea(tarea: NuevaTarea, request: Request) -> dict:
    job = await jobs.crear(request.app.state.redis, tarea.consulta, tarea.thread_id)
    return {"job_id": job["job_id"], "thread_id": job["thread_id"], "estado": job["estado"]}


@app.get("/tasks/{job_id}")
async def estado_tarea(job_id: str, request: Request) -> dict:
    job = await jobs.obtener(request.app.state.redis, job_id)
    if job is None:
        raise HTTPException(404, "No existe la tarea")
    return job


@app.get("/ordenes")
async def ordenes(request: Request) -> list[dict]:
    return await jobs.listar_ordenes(request.app.state.redis)


@app.get("/health")
async def health(request: Request) -> dict:
    await request.app.state.redis.ping()
    return {"ok": True}

import json
import os

os.environ.setdefault("GEMINI_API_KEY", "test")

import fakeredis
import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app import jobs
from app.config import get_settings
from app.hitl import aprobacion_humana
from app.main import app
from app.worker import Worker


@pytest.fixture
async def redis():
    cliente = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield cliente
    await cliente.aclose()


@pytest.fixture
async def api(redis):
    app.state.redis = redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as cliente:
        yield cliente


class EstadoPrueba(TypedDict, total=False):
    orden: dict
    decision: dict
    respuesta: str


def grafo_con_aprobacion():
    async def responder(state: EstadoPrueba) -> dict:
        return {"respuesta": "aprobada" if state["decision"]["aprobado"] else "rechazada"}

    g = StateGraph(EstadoPrueba)
    g.add_node("aprobacion", aprobacion_humana)
    g.add_node("responder", responder)
    g.add_edge(START, "aprobacion")
    g.add_edge("aprobacion", "responder")
    g.add_edge("responder", END)
    return g.compile(checkpointer=InMemorySaver())


class GrafoRoto:
    async def ainvoke(self, *args, **kwargs):
        raise RuntimeError("se cayó el LLM")


async def test_crear_tarea_queda_pending_y_encolada(api, redis):
    r = await api.post("/tasks", json={"consulta": "comparar monitores"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    job = (await api.get(f"/tasks/{job_id}")).json()
    assert job["estado"] == "PENDING"
    assert json.loads(await redis.rpop(jobs.COLA)) == {"job_id": job_id, "tipo": "nuevo"}


async def test_tarea_inexistente(api):
    assert (await api.get("/tasks/no-existe")).status_code == 404


async def test_error_del_grafo_marca_failed(redis):
    job = await jobs.crear(redis, "comparar monitores")
    worker = Worker(get_settings(), redis, GrafoRoto())
    await worker.procesar({"job_id": job["job_id"], "tipo": "nuevo"})

    guardado = await jobs.obtener(redis, job["job_id"])
    assert guardado["estado"] == "FAILED"
    assert "se cayó el LLM" in guardado["error"]


async def test_orden_critica_espera_aprobacion_y_se_reanuda(api, redis):
    grafo = grafo_con_aprobacion()
    worker = Worker(get_settings(), redis, grafo)
    job = await jobs.crear(redis, "orden grande")
    config = {"configurable": {"thread_id": job["thread_id"]}}

    await grafo.ainvoke({"orden": {"total": 900_000}}, config)
    await worker._guardar_resultado(job["job_id"], config)
    assert (await jobs.obtener(redis, job["job_id"]))["estado"] == "WAITING_APPROVAL"

    r = await api.post(f"/tasks/{job['job_id']}/approve", json={"aprobado": False, "comentario": "muy caro"})
    assert r.status_code == 202
    assert (await api.post(f"/tasks/{job['job_id']}/approve", json={"aprobado": True})).status_code == 409

    await redis.rpop(jobs.COLA)
    mensaje = json.loads(await redis.rpop(jobs.COLA))
    assert mensaje["tipo"] == "reanudar"
    await worker.procesar(mensaje)

    final = await jobs.obtener(redis, job["job_id"])
    assert final["estado"] == "DONE"
    assert final["resultado"] == "rechazada"


async def test_orden_chica_no_pide_aprobacion():
    grafo = grafo_con_aprobacion()
    salida = await grafo.ainvoke({"orden": {"total": 1000}}, {"configurable": {"thread_id": "t"}})
    assert salida["respuesta"] == "aprobada"


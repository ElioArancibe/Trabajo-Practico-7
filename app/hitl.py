from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from langgraph.types import interrupt
from pydantic import BaseModel

from . import jobs
from .config import get_settings
from .jobs import EstadoJob


def es_critica(orden: dict) -> bool:
    return orden["total"] >= get_settings().umbral_aprobacion


async def aprobacion_humana(state: dict) -> dict:
    orden = state["orden"]
    if not es_critica(orden):
        return {"decision": {"aprobado": True, "por": "automatico"}}

    # el grafo se corta acá; el checkpoint queda en Redis hasta que llegue el POST /approve
    umbral = f"{get_settings().umbral_aprobacion:,}".replace(",", ".")
    respuesta = interrupt({
        "motivo": f"La orden supera el monto que requiere aprobación (${umbral})",
        "orden": orden,
    })
    return {"decision": {"aprobado": bool(respuesta.get("aprobado")), "comentario": respuesta.get("comentario"), "por": "humano"}}


def ruta_aprobacion(state: dict) -> Literal["ejecutar_orden", "supervisor"]:
    return "ejecutar_orden" if state["decision"]["aprobado"] else "supervisor"


class Aprobacion(BaseModel):
    aprobado: bool
    comentario: str | None = None


router = APIRouter()


@router.post("/tasks/{job_id}/approve", status_code=202)
async def aprobar(job_id: str, aprobacion: Aprobacion, request: Request) -> dict:
    redis = request.app.state.redis
    job = await jobs.obtener(redis, job_id)
    if job is None:
        raise HTTPException(404, "No existe la tarea")
    if job["estado"] != EstadoJob.WAITING_APPROVAL:
        raise HTTPException(409, f"La tarea no está esperando aprobación (estado: {job['estado']})")
    if not await redis.set(f"tp7:aprobacion:{job_id}", 1, nx=True, ex=3600):
        raise HTTPException(409, "La tarea ya recibió una decisión")

    decision = aprobacion.model_dump()
    await jobs.actualizar(redis, job_id, EstadoJob.PENDING, decision=decision, pendiente=None)
    await jobs.encolar(redis, {"job_id": job_id, "tipo": "reanudar", "decision": decision})
    return {"job_id": job_id, "estado": EstadoJob.PENDING}

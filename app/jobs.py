import json
import uuid
from datetime import datetime
from enum import StrEnum

from redis.asyncio import Redis

COLA = "tp7:cola"
ORDENES = "tp7:ordenes"
CAMPOS_JSON = {"pendiente", "orden", "decision"}


class EstadoJob(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"


def _clave(job_id: str) -> str:
    return f"tp7:job:{job_id}"


def _ahora() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


async def crear(redis: Redis, consulta: str, thread_id: str | None = None) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "thread_id": thread_id or job_id,
        "consulta": consulta,
        "estado": EstadoJob.PENDING,
        "creado": _ahora(),
        "actualizado": _ahora(),
    }
    # el estado se guarda antes de encolar para que el GET nunca devuelva 404
    await redis.hset(_clave(job_id), mapping=job)
    await encolar(redis, {"job_id": job_id, "tipo": "nuevo"})
    return job


async def encolar(redis: Redis, mensaje: dict) -> None:
    await redis.lpush(COLA, json.dumps(mensaje))


async def obtener(redis: Redis, job_id: str) -> dict | None:
    job = await redis.hgetall(_clave(job_id))
    if not job:
        return None
    for campo in CAMPOS_JSON & job.keys():
        job[campo] = json.loads(job[campo])
    return job


async def actualizar(redis: Redis, job_id: str, estado: EstadoJob, **campos) -> None:
    datos = {"estado": estado, "actualizado": _ahora()}
    borrar = [campo for campo, valor in campos.items() if valor is None]
    for campo, valor in campos.items():
        if valor is not None:
            datos[campo] = json.dumps(valor, ensure_ascii=False) if campo in CAMPOS_JSON else str(valor)
    await redis.hset(_clave(job_id), mapping=datos)
    if borrar:
        await redis.hdel(_clave(job_id), *borrar)


async def guardar_orden(redis: Redis, orden: dict) -> str:
    numero = await redis.incr(f"{ORDENES}:seq")
    orden_id = f"OC-{numero:05d}"
    await redis.hset(ORDENES, orden_id, json.dumps({**orden, "orden_id": orden_id, "fecha": _ahora()}, ensure_ascii=False))
    return orden_id


async def listar_ordenes(redis: Redis) -> list[dict]:
    ordenes = await redis.hgetall(ORDENES)
    return sorted((json.loads(o) for o in ordenes.values()), key=lambda o: o["orden_id"])

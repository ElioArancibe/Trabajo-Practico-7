import asyncio
import json
import logging

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.types import Command
from openinference.instrumentation import using_attributes
from redis.asyncio import Redis

from . import jobs
from .config import Settings, get_settings
from .graph import construir_grafo, entrada_inicial
from .jobs import EstadoJob
from .observability import iniciar_trazas

log = logging.getLogger("worker")


class Worker:
    def __init__(self, settings: Settings, redis: Redis, grafo, tracer=None):
        self.settings = settings
        self.redis = redis
        self.grafo = grafo
        self.tracer = tracer

    async def procesar(self, mensaje: dict) -> None:
        job_id = mensaje["job_id"]
        job = await jobs.obtener(self.redis, job_id)
        if job is None:
            log.warning("job %s no existe, se descarta", job_id)
            return

        config = {"configurable": {"thread_id": job["thread_id"]}, "recursion_limit": 40}
        if mensaje["tipo"] == "reanudar":
            entrada = Command(resume=mensaje["decision"])
        else:
            entrada = entrada_inicial(job["consulta"])

        await jobs.actualizar(self.redis, job_id, EstadoJob.RUNNING)
        try:
            async with asyncio.timeout(self.settings.timeout_job):
                with using_attributes(session_id=job["thread_id"], metadata={"job_id": job_id, "tipo": mensaje["tipo"]}):
                    await self._correr(job, mensaje, entrada, config)
            await self._guardar_resultado(job_id, config)
        except Exception as exc:
            log.exception("job %s falló", job_id)
            detalle = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            await jobs.actualizar(self.redis, job_id, EstadoJob.FAILED, error=detalle)

    async def _correr(self, job: dict, mensaje: dict, entrada, config: dict) -> None:
        if self.tracer is None:
            await self.grafo.ainvoke(entrada, config)
            return
        nombre = "reanudar" if mensaje["tipo"] == "reanudar" else "tarea"
        with self.tracer.start_as_current_span(nombre, openinference_span_kind="chain") as span:
            span.set_input(mensaje.get("decision") or job["consulta"])
            salida = await self.grafo.ainvoke(entrada, config)
            span.set_output(salida.get("respuesta") or "esperando aprobación")

    async def _guardar_resultado(self, job_id: str, config: dict) -> None:
        snapshot = await self.grafo.aget_state(config)
        if snapshot.interrupts:
            await jobs.actualizar(self.redis, job_id, EstadoJob.WAITING_APPROVAL, pendiente=snapshot.interrupts[0].value)
            return
        valores = snapshot.values
        await jobs.actualizar(
            self.redis,
            job_id,
            EstadoJob.DONE,
            resultado=valores.get("respuesta") or "",
            orden=valores.get("orden"),
            orden_id=valores.get("orden_id"),
        )

    async def consumir(self, nro: int) -> None:
        log.info("consumidor %d escuchando %s", nro, jobs.COLA)
        while True:
            item = await self.redis.brpop([jobs.COLA], timeout=5)
            if item is None:
                continue
            try:
                mensaje = json.loads(item[1])
            except json.JSONDecodeError:
                log.error("mensaje inválido en la cola: %r", item[1])
                continue
            await self.procesar(mensaje)


async def main() -> None:
    settings = get_settings()
    provider = iniciar_trazas(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async with AsyncRedisSaver.from_conn_string(settings.redis_url) as checkpointer:
        await checkpointer.asetup()
        grafo = construir_grafo(settings, checkpointer, redis)
        worker = Worker(settings, redis, grafo, provider.get_tracer(__name__))
        try:
            await asyncio.gather(*(worker.consumir(i) for i in range(settings.worker_concurrencia)))
        finally:
            await redis.aclose()
            provider.force_flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())

import argparse
import asyncio
import statistics
import time

import httpx

CONSULTAS = [
    "Compará los proveedores de monitores de 24 pulgadas para 30 unidades y generá la orden con el más conveniente.",
    "¿Qué proveedor de teclados mecánicos conviene para 40 unidades? Solo quiero la comparación.",
    "Necesito 20 auriculares inalámbricos. Analizá las opciones y hacé el pedido al mejor.",
    "Quiero encargar 8 notebooks de 14 pulgadas al proveedor que mejor convenga.",
    "¿Cuánto me sale comprar 15 webcams Full HD con cada proveedor? Tené en cuenta las notas de mercado.",
]
FINALES = {"DONE", "FAILED"}


async def correr(cliente: httpx.AsyncClient, consulta: str, aprobar: bool) -> dict:
    inicio = time.perf_counter()
    r = await cliente.post("/tasks", json={"consulta": consulta})
    r.raise_for_status()
    job_id = r.json()["job_id"]

    while True:
        await asyncio.sleep(2)
        job = (await cliente.get(f"/tasks/{job_id}")).json()
        if job["estado"] == "WAITING_APPROVAL":
            if not aprobar:
                break
            await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True, "comentario": "aprobado en prueba de carga"})
        elif job["estado"] in FINALES:
            break

    job["segundos"] = round(time.perf_counter() - inicio, 1)
    return job


async def main(url: str, aprobar: bool) -> None:
    async with httpx.AsyncClient(base_url=url, timeout=30) as cliente:
        resultados = await asyncio.gather(*(correr(cliente, c, aprobar) for c in CONSULTAS))

    for job in resultados:
        print(f"{job['job_id'][:8]}  {job['estado']:<16} {job['segundos']:>6}s  {job.get('orden_id', '')}")
        print(f"   {job.get('resultado') or job.get('error') or job.get('pendiente')}\n")

    tiempos = sorted(j["segundos"] for j in resultados)
    print(f"latencia media {statistics.mean(tiempos):.1f}s | p95 {statistics.quantiles(tiempos, n=20, method='inclusive')[-1]:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lanza 5 tareas concurrentes contra la API")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--aprobar", action="store_true", help="aprueba automáticamente las órdenes que piden aprobación")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.aprobar))

# API de producción para el orquestador multi-agente

API FastAPI que expone el orquestador de compras del módulo 6 (supervisor + investigador + analista + compras). Las tareas se encolan en Redis y las procesa un worker aparte; los checkpoints del grafo se guardan con `AsyncRedisSaver` y las trazas van a Arize Phoenix.

```
supervisor ─┬─ investigador ─┐
            ├─ analista ─────┤→ supervisor
            ├─ compras → aprobacion (interrupt si total ≥ $500.000) → ejecutar_orden ─┘
            └─ sintesis → END
```

Estados del job: `PENDING → RUNNING → (WAITING_APPROVAL) → DONE | FAILED`.

## Levantar

```bash
cp .env.example .env      # completar GEMINI_API_KEY
docker compose up --build
```

API en `http://localhost:8000/docs`, Phoenix en `http://localhost:6006`.

Sin Docker para la app (Redis 8 y Phoenix igual tienen que estar corriendo):

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
docker compose up -d redis phoenix
uvicorn app.main:app
python -m app.worker        # en otra terminal
```

## Endpoints

| Método | Ruta | |
|---|---|---|
| POST | `/tasks` | `{"consulta": "...", "thread_id": opcional}` → `job_id` |
| GET | `/tasks/{id}` | estado, resultado u orden pendiente de aprobación |
| POST | `/tasks/{id}/approve` | `{"aprobado": true, "comentario": "..."}` |
| GET | `/ordenes` | órdenes de compra ejecutadas |

## Prueba de carga

```bash
python scripts/prueba_carga.py --aprobar
```

Manda 5 consultas concurrentes, aprueba las órdenes que quedan esperando y muestra la latencia de cada una. Las capturas de Phoenix (trazas, costo y latencia p95) están en [`screenshots/`](screenshots).

## Tests

```bash
pytest
```

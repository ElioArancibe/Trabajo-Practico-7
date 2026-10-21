import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    api_key: SecretStr
    model: str = "gemini-3.5-flash-lite"
    llm_rpm: int = Field(default=12, ge=1)
    redis_url: str = "redis://localhost:6379"
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_project: str = "tp7-orquestador"

    max_pasos: int = Field(default=8, ge=3, le=20)
    umbral_aprobacion: int = Field(default=500_000, ge=0)
    worker_concurrencia: int = Field(default=5, ge=1)
    timeout_job: int = Field(default=240, ge=10)

    catalogo: Path = BASE_DIR / "data" / "catalogo.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Falta GEMINI_API_KEY en el .env (ver .env.example).")

    overrides = {
        "model": os.getenv("GEMINI_MODEL"),
        "llm_rpm": os.getenv("LLM_RPM"),
        "redis_url": os.getenv("REDIS_URL"),
        "phoenix_endpoint": os.getenv("PHOENIX_ENDPOINT"),
        "phoenix_project": os.getenv("PHOENIX_PROJECT"),
        "umbral_aprobacion": os.getenv("UMBRAL_APROBACION"),
        "worker_concurrencia": os.getenv("WORKER_CONCURRENCIA"),
    }
    return Settings(api_key=api_key, **{k: v for k, v in overrides.items() if v})

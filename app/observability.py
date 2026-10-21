from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from phoenix.otel import register

from .config import Settings


def iniciar_trazas(settings: Settings) -> TracerProvider:
    provider = register(
        project_name=settings.phoenix_project,
        endpoint=settings.phoenix_endpoint,
        batch=True,
    )
    LangChainInstrumentor().instrument(tracer_provider=provider)
    return provider

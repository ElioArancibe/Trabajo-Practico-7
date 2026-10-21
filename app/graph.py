from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from . import jobs
from .config import Settings
from .hitl import aprobacion_humana, ruta_aprobacion
from .tools import TOOLS_ANALISTA, TOOLS_INVESTIGADOR, buscar_oferta, calcular_costo

PROMPT_SUPERVISOR = """Coordinás el equipo de compras de una tienda de tecnología. No resolvés las tareas: decidís quién trabaja.
- investigador: busca proveedores, precios, stock y notas de mercado.
- analista: calcula costos totales y compara ofertas. Necesita los datos del investigador.
- compras: arma la orden de compra. Solo si el usuario pidió comprar o encargar, el análisis ya recomienda un proveedor y todavía no hay orden.
- fin: cuando ya se puede responder la consulta, o cuando la orden ya fue ejecutada o rechazada.
Si un resultado está incompleto o no responde lo pedido, mandá de nuevo al especialista con una instrucción más precisa.
No pidas compras si el usuario solo quiere comparar o averiguar."""

PROMPT_INVESTIGADOR = """Sos el investigador de compras. Usá las herramientas para juntar los datos que pide la instrucción:
ofertas de proveedores (precio, stock, entrega, envío, descuentos, calificación) y notas de mercado relevantes.
Devolvé un resumen con los datos exactos, sin recomendar ni calcular totales."""

PROMPT_ANALISTA = """Sos el analista de compras. Con los datos de la investigación calculá el costo total de cada oferta
con la herramienta costo_total y puntuá las ofertas con puntaje_oferta. No inventes números.
Terminá con una recomendación clara: proveedor, producto, cantidad y total."""

PROMPT_COMPRAS = "Extraé la orden de compra que corresponde a la consulta según la recomendación del análisis."

PROMPT_SINTESIS = """Redactá la respuesta final para el usuario en español rioplatense, breve y con los montos exactos (formato $1.234.567).
Si se generó una orden, indicá su número. Si la orden fue rechazada, decilo e incluí el comentario del aprobador."""


class Estado(MessagesState):
    consulta: str
    siguiente: str
    instruccion: str
    investigacion: str | None
    analisis: str | None
    orden: dict | None
    decision: dict | None
    orden_id: str | None
    pasos: int
    respuesta: str | None


class Decision(BaseModel):
    siguiente: Literal["investigador", "analista", "compras", "fin"]
    instruccion: str = Field(description="Qué tiene que hacer el especialista elegido. Vacío si es fin.")


class OrdenPropuesta(BaseModel):
    proveedor: str
    producto: str
    cantidad: int = Field(gt=0)


def entrada_inicial(consulta: str) -> dict:
    return {
        "messages": [HumanMessage(consulta)],
        "consulta": consulta,
        "investigacion": None,
        "analisis": None,
        "orden": None,
        "decision": None,
        "orden_id": None,
        "pasos": 0,
        "respuesta": None,
    }


def _historial(mensajes: list[BaseMessage]) -> str:
    previos = mensajes[:-1][-4:]
    return "\n".join(f"{'Usuario' if isinstance(m, HumanMessage) else 'Asistente'}: {m.text}" for m in previos)


def _contexto(state: Estado, *campos: str) -> str:
    partes = [f"Consulta del usuario: {state['consulta']}"]
    if historial := _historial(state["messages"]):
        partes.append(f"Conversación previa:\n{historial}")
    etiquetas = {
        "investigacion": "Investigación",
        "analisis": "Análisis",
        "orden": "Orden propuesta",
        "decision": "Decisión de aprobación",
        "orden_id": "Orden ejecutada",
    }
    for campo in campos:
        partes.append(f"{etiquetas[campo]}:\n{state.get(campo) or '(sin datos)'}")
    return "\n\n".join(partes)


def _tarea(state: Estado, *campos: str) -> str:
    return f"Instrucción: {state['instruccion']}\n\n{_contexto(state, *campos)}"


def construir_grafo(settings: Settings, checkpointer=None, redis: Redis | None = None):
    llm = ChatGoogleGenerativeAI(
        model=settings.model,
        api_key=settings.api_key,
        max_retries=4,
        # free tier de Gemini: todos los agentes comparten el mismo cupo por minuto
        rate_limiter=InMemoryRateLimiter(requests_per_second=settings.llm_rpm / 60, max_bucket_size=2),
    )
    supervisor_llm = llm.with_structured_output(Decision)
    compras_llm = llm.with_structured_output(OrdenPropuesta)
    agente_investigador = create_agent(llm, TOOLS_INVESTIGADOR, system_prompt=PROMPT_INVESTIGADOR, name="investigador")
    agente_analista = create_agent(llm, TOOLS_ANALISTA, system_prompt=PROMPT_ANALISTA, name="analista")

    async def supervisor(state: Estado) -> dict:
        pasos = state["pasos"] + 1
        if pasos > settings.max_pasos:
            return {"siguiente": "fin", "pasos": pasos}

        contexto = _contexto(state, "investigacion", "analisis", "orden", "decision", "orden_id")
        decision: Decision = await supervisor_llm.ainvoke([SystemMessage(PROMPT_SUPERVISOR), HumanMessage(contexto)])
        siguiente = decision.siguiente
        if siguiente == "compras" and state.get("decision"):
            siguiente = "fin"
        return {"siguiente": siguiente, "instruccion": decision.instruccion, "pasos": pasos}

    async def especialista(agente, state: Estado, *campos: str) -> str:
        resultado = await agente.ainvoke({"messages": [HumanMessage(_tarea(state, *campos))]}, {"recursion_limit": 12})
        return resultado["messages"][-1].text

    async def investigador(state: Estado) -> dict:
        return {"investigacion": await especialista(agente_investigador, state, "investigacion")}

    async def analista(state: Estado) -> dict:
        return {"analisis": await especialista(agente_analista, state, "investigacion", "analisis")}

    async def compras(state: Estado) -> dict:
        propuesta: OrdenPropuesta = await compras_llm.ainvoke(
            [SystemMessage(PROMPT_COMPRAS), HumanMessage(_tarea(state, "analisis", "orden"))]
        )
        # los montos se recalculan con el catálogo, no se toman del LLM
        oferta = buscar_oferta(propuesta.proveedor, propuesta.producto)
        if oferta is None:
            return {"orden": {"error": f"{propuesta.proveedor} no vende '{propuesta.producto}'"}}
        if propuesta.cantidad > oferta["stock"]:
            return {"orden": {"error": f"{oferta['proveedor']} tiene {oferta['stock']} unidades, se pidieron {propuesta.cantidad}"}}
        return {"orden": calcular_costo(oferta, propuesta.cantidad)}

    def ruta_compras(state: Estado) -> Literal["aprobacion", "supervisor"]:
        return "supervisor" if "error" in state["orden"] else "aprobacion"

    async def ejecutar_orden(state: Estado) -> dict:
        orden_id = await jobs.guardar_orden(redis, {**state["orden"], "aprobacion": state["decision"]})
        return {"orden_id": orden_id}

    async def sintesis(state: Estado) -> dict:
        contexto = _contexto(state, "investigacion", "analisis", "orden", "decision", "orden_id")
        respuesta = await llm.ainvoke([SystemMessage(PROMPT_SINTESIS), HumanMessage(contexto)])
        return {"respuesta": respuesta.text, "messages": [AIMessage(respuesta.text, name="sintesis")]}

    grafo = StateGraph(Estado)
    grafo.add_node("supervisor", supervisor)
    grafo.add_node("investigador", investigador)
    grafo.add_node("analista", analista)
    grafo.add_node("compras", compras)
    grafo.add_node("aprobacion", aprobacion_humana)
    grafo.add_node("ejecutar_orden", ejecutar_orden)
    grafo.add_node("sintesis", sintesis)

    grafo.add_edge(START, "supervisor")
    grafo.add_conditional_edges(
        "supervisor",
        lambda s: s["siguiente"],
        {"investigador": "investigador", "analista": "analista", "compras": "compras", "fin": "sintesis"},
    )
    grafo.add_edge("investigador", "supervisor")
    grafo.add_edge("analista", "supervisor")
    grafo.add_conditional_edges("compras", ruta_compras)
    grafo.add_conditional_edges("aprobacion", ruta_aprobacion)
    grafo.add_edge("ejecutar_orden", "supervisor")
    grafo.add_edge("sintesis", END)
    return grafo.compile(checkpointer=checkpointer)

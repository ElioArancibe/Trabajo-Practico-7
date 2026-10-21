import json
import unicodedata
from functools import lru_cache

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .config import get_settings


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in texto if not unicodedata.combining(c)).strip()


def _singular(palabra: str) -> str:
    if palabra.endswith("es") and len(palabra) > 6:
        return palabra[:-2]
    return palabra.removesuffix("s")


def coincide(buscado: str, producto: str) -> bool:
    palabras = [_singular(p) for p in _normalizar(buscado).split() if len(p) > 3]
    return all(p in _normalizar(producto) for p in palabras)


@lru_cache(maxsize=1)
def catalogo() -> dict:
    return json.loads(get_settings().catalogo.read_text(encoding="utf-8"))


def buscar_oferta(proveedor: str, producto: str) -> dict | None:
    for oferta in catalogo()["proveedores"]:
        if _normalizar(oferta["proveedor"]) == _normalizar(proveedor) and coincide(producto, oferta["producto"]):
            return oferta
    return None


def calcular_costo(oferta: dict, cantidad: int) -> dict:
    subtotal = oferta["precio_unitario"] * cantidad
    pct = oferta["descuento"]["pct"] if cantidad >= oferta["descuento"]["desde"] else 0
    descuento = round(subtotal * pct / 100)
    return {
        "proveedor": oferta["proveedor"],
        "producto": oferta["producto"],
        "cantidad": cantidad,
        "subtotal": subtotal,
        "descuento_pct": pct,
        "descuento": descuento,
        "envio": oferta["envio"],
        "total": subtotal - descuento + oferta["envio"],
        "costo_por_unidad": round((subtotal - descuento + oferta["envio"]) / cantidad),
    }


class ProductoInput(BaseModel):
    producto: str = Field(min_length=3, description="Producto a buscar, ej. 'monitor', 'teclado mecánico'")


class TemaInput(BaseModel):
    tema: str = Field(min_length=3, description="Tema o palabra clave, ej. 'monitores', 'logistica', 'ImportPC'")


class CostoInput(BaseModel):
    proveedor: str = Field(description="Nombre exacto del proveedor, ej. 'ImportPC'")
    producto: str = Field(description="Producto, ej. 'Monitor 24 pulgadas'")
    cantidad: int = Field(gt=0, le=10_000)


class PuntajeInput(BaseModel):
    costo_por_unidad: int = Field(gt=0)
    entrega_dias: int = Field(ge=0)
    calificacion: float = Field(ge=0, le=5)
    costo_referencia: int = Field(gt=0, description="El menor costo por unidad entre las ofertas comparadas")


@tool(args_schema=ProductoInput)
async def buscar_proveedores(producto: str) -> dict:
    """Busca en el catálogo de compras los proveedores que venden un producto.

    Devuelve precio unitario, stock, días de entrega, costo de envío, descuento por
    volumen (desde cuántas unidades y qué porcentaje) y calificación de cada proveedor.
    """
    ofertas = [o for o in catalogo()["proveedores"] if coincide(producto, o["producto"])]
    if not ofertas:
        productos = sorted({o["producto"] for o in catalogo()["proveedores"]})
        return {"error": f"No hay proveedores para '{producto}'.", "productos_disponibles": productos}
    return {"ofertas": ofertas}


@tool(args_schema=TemaInput)
async def buscar_notas_mercado(tema: str) -> dict:
    """Busca notas de mercado internas: tendencias de precios, reclamos, logística y novedades de proveedores."""
    buscado = _normalizar(tema)
    notas = [n["texto"] for n in catalogo()["notas"] if buscado in _normalizar(n["tema"] + " " + n["texto"])]
    if not notas:
        temas = sorted({n["tema"] for n in catalogo()["notas"]})
        return {"error": f"Sin notas sobre '{tema}'.", "temas_disponibles": temas}
    return {"notas": notas}


@tool(args_schema=CostoInput)
async def costo_total(proveedor: str, producto: str, cantidad: int) -> dict:
    """Calcula el costo total de comprar una cantidad a un proveedor: subtotal, descuento por volumen, envío y costo final por unidad."""
    oferta = buscar_oferta(proveedor, producto)
    if oferta is None:
        return {"error": f"{proveedor} no vende '{producto}'. Revisá los nombres con los datos de la investigación."}
    resultado = calcular_costo(oferta, cantidad)
    if cantidad > oferta["stock"]:
        resultado["advertencia"] = f"Stock insuficiente: el proveedor tiene {oferta['stock']} unidades."
    return resultado


@tool(args_schema=PuntajeInput)
async def puntaje_oferta(costo_por_unidad: int, entrega_dias: int, calificacion: float, costo_referencia: int) -> dict:
    """Puntúa una oferta de 0 a 100 combinando precio (50%), tiempo de entrega (25%) y calificación del proveedor (25%)."""
    precio = min(costo_referencia / costo_por_unidad, 1) * 50
    entrega = max(0, 1 - entrega_dias / 15) * 25
    reputacion = calificacion / 5 * 25
    return {"puntaje": round(precio + entrega + reputacion, 1)}


TOOLS_INVESTIGADOR = [buscar_proveedores, buscar_notas_mercado]
TOOLS_ANALISTA = [costo_total, puntaje_oferta]

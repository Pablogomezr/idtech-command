"""Disponibilidad y cumplimiento de SLA (ADR 0004). Funciones puras: sin base de datos.

Reglas fijadas aquí (las pruebas las protegen; cambiarlas cambia lo que se le cobra al cliente):

* Un equipo está CAÍDO si alguno de sus chequeos está caído; ARRIBA si ninguno está caído y al
  menos uno está ok o degradado (degradado sigue prestando servicio y se informa aparte).
  Si todos sus chequeos están en desconocido, ese tiempo es SIN DATOS.
* Cada chequeo cuenta solo en las horas en que envió agregados; fuera de ellas es desconocido
  aunque su último estado fuera ok (agente apagado, sin internet...) o caído (chequeo retirado:
  no se cobra para siempre). Si ningún chequeo del equipo tiene datos, el tiempo es SIN DATOS:
  no es caída (supuesto P3), se informa aparte y no entra en la fórmula.
* Las exclusiones (mantenimiento, causa del cliente) se descuentan primero; lo excluido no es
  caída ni sin datos.
* disponibilidad = arriba / (arriba + caído). Antes de que el equipo empiece a reportar no se
  mide nada.
* Sitio: promedio de los equipos ponderado por criticidad (1 = crítico pesa 3; 3 pesa 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

UNA_HORA = timedelta(hours=1)
PESO_CRITICIDAD = {1: 3, 2: 2, 3: 1}

Tramo = tuple[datetime, datetime]
Segmento = tuple[datetime, datetime, str]  # (inicio, fin, estado)


def truncar_hora(momento: datetime) -> datetime:
    return momento.replace(minute=0, second=0, microsecond=0)


def linea_de_tiempo(
    estado_inicial: str | None,
    cambios: Iterable[tuple[datetime, str]],
    desde: datetime,
    hasta: datetime,
) -> list[Segmento]:
    """Estado de UN chequeo en [desde, hasta). `estado_inicial` es el del último evento anterior
    a `desde` (None si no hay: desconocido hasta el primer cambio)."""
    segmentos: list[Segmento] = []
    actual, inicio = estado_inicial or "desconocido", desde
    for momento, estado in sorted(cambios):
        momento = min(max(momento, desde), hasta)
        if momento > inicio:
            segmentos.append((inicio, momento, actual))
        actual, inicio = estado, max(inicio, momento)
    if hasta > inicio:
        segmentos.append((inicio, hasta, actual))
    return segmentos


def _estado_en(segmentos: Sequence[Segmento], momento: datetime) -> str:
    for inicio, fin, estado in segmentos:
        if inicio <= momento < fin:
            return estado
    return "desconocido"


def combinar(
    lineas: Sequence[Sequence[Segmento]], desde: datetime, hasta: datetime
) -> list[Segmento]:
    """Estado del EQUIPO a partir de las líneas de tiempo de sus chequeos."""
    cortes = sorted({desde, hasta} | {m for linea in lineas for s in linea for m in s[:2]})
    cortes = [c for c in cortes if desde <= c <= hasta]
    salida: list[Segmento] = []
    for inicio, fin in zip(cortes, cortes[1:], strict=False):
        estados = {_estado_en(linea, inicio) for linea in lineas}
        if "caido" in estados:
            estado = "caido"
        elif "degradado" in estados:
            estado = "degradado"
        elif "ok" in estados:
            estado = "ok"
        else:
            estado = "desconocido"
        if salida and salida[-1][2] == estado and salida[-1][1] == inicio:
            salida[-1] = (salida[-1][0], fin, estado)
        else:
            salida.append((inicio, fin, estado))
    return salida


def horas_sin_datos(
    horas_con_datos: set[datetime], desde: datetime, hasta: datetime
) -> list[Tramo]:
    """Horas completas (o el pedazo dentro de [desde, hasta)) sin ningún agregado del equipo."""
    tramos: list[Tramo] = []
    hora = truncar_hora(desde)
    while hora < hasta:
        if hora not in horas_con_datos:
            inicio, fin = max(hora, desde), min(hora + UNA_HORA, hasta)
            if tramos and tramos[-1][1] == inicio:
                tramos[-1] = (tramos[-1][0], fin)
            else:
                tramos.append((inicio, fin))
        hora += UNA_HORA
    return tramos


def _dentro(tramos: Sequence[Tramo], inicio: datetime) -> bool:
    return any(a <= inicio < b for a, b in tramos)


def aplicar_sin_datos(linea: Sequence[Segmento], sin_datos: Sequence[Tramo]) -> list[Segmento]:
    """La línea de un chequeo con sus horas sin agregados convertidas en desconocido."""
    if not sin_datos:
        return list(linea)
    salida: list[Segmento] = []
    for inicio, fin, estado in linea:
        cortes = sorted({inicio, fin} | {x for t in sin_datos for x in t if inicio < x < fin})
        for a, b in zip(cortes, cortes[1:], strict=False):
            e = "desconocido" if _dentro(sin_datos, a) else estado
            if salida and salida[-1][2] == e and salida[-1][1] == a:
                salida[-1] = (salida[-1][0], b, e)
            else:
                salida.append((a, b, e))
    return salida


@dataclass
class Medicion:
    arriba_s: float = 0.0  # incluye el tiempo degradado
    degradado_s: float = 0.0
    caido_s: float = 0.0
    sin_datos_s: float = 0.0
    excluido_s: float = 0.0
    caidas: list[Tramo] = field(default_factory=list)

    @property
    def medible_s(self) -> float:
        return self.arriba_s + self.caido_s

    @property
    def disponibilidad(self) -> float | None:
        """Porcentaje, o None si no hubo tiempo medible."""
        if self.medible_s <= 0:
            return None
        return 100.0 * self.arriba_s / self.medible_s


def medir(
    linea: Sequence[Segmento],
    sin_datos: Sequence[Tramo],
    exclusiones: Sequence[Tramo],
    desde: datetime,
    hasta: datetime,
) -> Medicion:
    """Clasifica cada instante de [desde, hasta): excluido > sin datos > estado del equipo."""
    m = Medicion()
    if hasta <= desde:
        return m
    cortes = {desde, hasta}
    for tramos in (sin_datos, exclusiones):
        cortes |= {x for t in tramos for x in t}
    cortes |= {x for s in linea for x in s[:2]}
    cortes = sorted(c for c in cortes if desde <= c <= hasta)
    for inicio, fin in zip(cortes, cortes[1:], strict=False):
        duracion = (fin - inicio).total_seconds()
        if _dentro(exclusiones, inicio):
            m.excluido_s += duracion
            continue
        estado = "desconocido" if _dentro(sin_datos, inicio) else _estado_en(linea, inicio)
        if estado == "caido":
            m.caido_s += duracion
            if m.caidas and m.caidas[-1][1] == inicio:
                m.caidas[-1] = (m.caidas[-1][0], fin)
            else:
                m.caidas.append((inicio, fin))
        elif estado in ("ok", "degradado"):
            m.arriba_s += duracion
            if estado == "degradado":
                m.degradado_s += duracion
        else:
            m.sin_datos_s += duracion
    return m


def ponderada(mediciones: Iterable[tuple[float | None, int]]) -> float | None:
    """Promedio de disponibilidades ponderado por criticidad; ignora equipos sin medición."""
    suma = pesos = 0.0
    for disponibilidad, criticidad in mediciones:
        if disponibilidad is None:
            continue
        peso = PESO_CRITICIDAD.get(criticidad, 2)
        suma += disponibilidad * peso
        pesos += peso
    return suma / pesos if pesos else None


# --- Tickets ---------------------------------------------------------------------------------


@dataclass
class Cumplimiento:
    cumplidos: int = 0
    incumplidos: int = 0
    pendientes: int = 0

    @property
    def porcentaje(self) -> float | None:
        evaluados = self.cumplidos + self.incumplidos
        return 100.0 * self.cumplidos / evaluados if evaluados else None


def _evaluar(hecho: datetime | None, vence: datetime, ahora: datetime, c: Cumplimiento) -> None:
    if hecho is not None:
        if hecho <= vence:
            c.cumplidos += 1
        else:
            c.incumplidos += 1
    elif ahora > vence:
        c.incumplidos += 1
    else:
        c.pendientes += 1


def indicadores_tickets(tickets: Sequence[dict], ahora: datetime) -> dict:
    """Cumplimiento por prioridad. No se evalúan los cancelados ni los que se recuperaron solos
    (cuentan como caída en la disponibilidad, pero no fueron un correctivo)."""
    por_prioridad: dict[str, dict] = {}
    respuestas, soluciones = [], []
    for t in tickets:
        p = por_prioridad.setdefault(
            t["prioridad"],
            {"total": 0, "no_aplica": 0, "respuesta": Cumplimiento(), "solucion": Cumplimiento()},
        )
        p["total"] += 1
        if t["cierre"] in ("cancelado", "recuperado_solo"):
            p["no_aplica"] += 1
            continue
        _evaluar(t["aceptado_at"], t["vence_respuesta_at"], ahora, p["respuesta"])
        _evaluar(t["resuelto_at"], t["vence_solucion_at"], ahora, p["solucion"])
        if t["aceptado_at"]:
            respuestas.append((t["aceptado_at"] - t["abierto_at"]).total_seconds())
        if t["cierre"] == "verificado" and t["resuelto_at"]:
            soluciones.append((t["resuelto_at"] - t["abierto_at"]).total_seconds())

    def total(clave: str) -> Cumplimiento:
        c = Cumplimiento()
        for p in por_prioridad.values():
            c.cumplidos += p[clave].cumplidos
            c.incumplidos += p[clave].incumplidos
            c.pendientes += p[clave].pendientes
        return c

    return {
        "por_prioridad": por_prioridad,
        "respuesta": total("respuesta"),
        "solucion": total("solucion"),
        "respuesta_prom_s": sum(respuestas) / len(respuestas) if respuestas else None,
        "mttr_s": sum(soluciones) / len(soluciones) if soluciones else None,
    }

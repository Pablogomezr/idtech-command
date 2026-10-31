"""Indicadores de gestión del servicio: rendimiento de cada técnico y confiabilidad de cada
equipo. Funciones puras (sin base de datos); las definiciones las fijan las pruebas.

Definiciones:
* Retrabajo: un servicio cerrado como reparado tras el cual el MISMO equipo vuelve a fallar con
  la MISMA falla dentro de VENTANA_RETRABAJO. Cuenta contra quien lo cerró.
* Resolución a la primera: reparados sin reapertura y sin retrabajo / reparados.
* Reincidencia de un equipo: un servicio que se abre dentro de VENTANA_RETRABAJO después del
  cierre del anterior del mismo equipo (cualquier falla).
* Cada cuánto falla (MTBF): tiempo medido del equipo / número de caídas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from . import calculo

VENTANA_RETRABAJO = timedelta(days=30)


def _promedio(valores: Iterable[float]) -> float | None:
    v = list(valores)
    return sum(v) / len(v) if v else None


def retrabajos(tickets: Sequence[dict]) -> set:
    """Ids de los servicios reparados a los que siguió la misma falla en el mismo equipo."""
    por_falla: dict[tuple, list[dict]] = defaultdict(list)
    for t in tickets:
        if t.get("dispositivo_id"):
            por_falla[(t["dispositivo_id"], t["codigo"])].append(t)
    salida = set()
    for lista in por_falla.values():
        lista.sort(key=lambda t: t["abierto_at"])
        for previo, siguiente in zip(lista, lista[1:], strict=False):
            if (
                previo["cierre"] == "verificado"
                and previo["cerrado_at"] is not None
                and timedelta(0)
                <= siguiente["abierto_at"] - previo["cerrado_at"]
                <= VENTANA_RETRABAJO
            ):
                salida.add(previo["id"])
    return salida


def reincidencias(tickets: Sequence[dict]) -> set:
    """Ids de los servicios abiertos poco después de cerrado el anterior del mismo equipo."""
    por_equipo: dict = defaultdict(list)
    for t in tickets:
        if t.get("dispositivo_id"):
            por_equipo[t["dispositivo_id"]].append(t)
    salida = set()
    for lista in por_equipo.values():
        lista.sort(key=lambda t: t["abierto_at"])
        for previo, siguiente in zip(lista, lista[1:], strict=False):
            cierre = previo["cerrado_at"]
            if (
                cierre is not None
                and timedelta(0) <= siguiente["abierto_at"] - cierre <= VENTANA_RETRABAJO
            ):
                salida.add(siguiente["id"])
    return salida


def rendimiento_tecnicos(
    tickets: Sequence[dict], todos: Sequence[dict], reabiertos: set, ahora: datetime
) -> list[dict]:
    """`tickets`: los del periodo (con `tecnico`); `todos`: también los posteriores, para ver si
    la falla volvió; `reabiertos`: ids con al menos una reapertura."""
    rehechos = retrabajos(todos)
    por_tecnico: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        por_tecnico[t["tecnico"] or "Sin asignar"].append(t)
    filas = []
    for nombre, lista in por_tecnico.items():
        ind = calculo.indicadores_tickets(lista, ahora)
        reparados = [t for t in lista if t["cierre"] == "verificado"]
        a_la_primera = [
            t for t in reparados if t["id"] not in reabiertos and t["id"] not in rehechos
        ]
        filas.append({
            "tecnico": nombre,
            "servicios": len(lista),
            "atendidos": sum(t["aceptado_at"] is not None for t in lista),
            "reparados": len(reparados),
            "en_curso": sum(t["estado"] not in ("cerrado", "cancelado") for t in lista),
            "respuesta_s": _promedio((t["aceptado_at"] - t["abierto_at"]).total_seconds()
                                     for t in lista if t["aceptado_at"]),
            "llegada_s": _promedio((t["en_sitio_at"] - t["abierto_at"]).total_seconds()
                                   for t in lista if t["en_sitio_at"]),
            "solucion_s": ind["mttr_s"],
            "respuesta_pct": ind["respuesta"].porcentaje,
            "solucion_pct": ind["solucion"].porcentaje,
            "reaperturas": sum(t["id"] in reabiertos for t in lista),
            "retrabajos": sum(t["id"] in rehechos for t in lista),
            "primera_pct": 100.0 * len(a_la_primera) / len(reparados) if reparados else None,
        })  # fmt: skip
    filas.sort(key=lambda f: (-f["servicios"], f["tecnico"]))
    return filas


def confiabilidad_equipos(
    equipos: Sequence[dict],
    mediciones: dict,
    tickets: Sequence[dict],
    todos: Sequence[dict],
) -> list[dict]:
    """Por equipo: caídas, cada cuánto falla, cuánto duran, servicios, visitas y reincidencias."""
    vuelve = reincidencias(todos)
    servicios: dict = defaultdict(list)
    for t in tickets:
        if t.get("dispositivo_id"):
            servicios[t["dispositivo_id"]].append(t)
    filas = []
    for e in equipos:
        m = mediciones.get(e["id"])
        lista = servicios.get(e["id"], [])
        caidas = len(m.caidas) if m else 0
        filas.append({
            "equipo": e["nombre"], "sitio": e.get("sitio"), "tipo": e["tipo"],
            "medido": m is not None,
            "disponibilidad": m.disponibilidad if m else None,
            "caidas": caidas,
            "caido_s": m.caido_s if m else 0.0,
            "mtbf_s": m.medible_s / caidas if m and caidas else None,
            "mttr_s": m.caido_s / caidas if m and caidas else None,
            "ultima_caida": max((a for a, _ in m.caidas), default=None) if m else None,
            "servicios": len(lista),
            "visitas": sum(t["en_sitio_at"] is not None for t in lista),
            "reincidencias": sum(t["id"] in vuelve for t in lista),
        })  # fmt: skip
    filas.sort(key=lambda f: (-f["caidas"], -f["servicios"], f["equipo"]))
    return filas

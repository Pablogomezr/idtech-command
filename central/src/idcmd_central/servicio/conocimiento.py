"""Base de conocimiento de fallas: los pasos que el técnico sigue en campo.

La fuente es `docs/plantillas/base_conocimiento.csv`; la central lleva una copia dentro del
paquete (la imagen Docker no incluye `docs/`). Una prueba exige que sean idénticas.
Se elige la fila más específica que coincida: marca + tipo de equipo > tipo > genérica.
"""

from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

ARCHIVO = Path(__file__).with_name("base_conocimiento.csv")
GENERICOS = ["Diagnosticar la falla en el equipo", "Corregir y probar que el equipo funcione"]


@cache
def _filas() -> list[dict]:
    with ARCHIVO.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _coincide(fila: dict, codigo: str) -> bool:
    disparadores = {d.strip() for d in fila["disparadores"].replace("|", ",").split(",")}
    return codigo in disparadores or fila["codigo_falla"] == codigo


def pasos(codigo: str, tipo_equipo: str | None = None, marca: str | None = None) -> list[str]:
    candidatas = [f for f in _filas() if _coincide(f, codigo)]

    def especificidad(f: dict) -> int:
        tipo_ok = f["tipo_equipo"] in ("", tipo_equipo)
        marca_ok = f["marca"] in ("", (marca or "").lower())
        if not (tipo_ok and marca_ok):
            return -1
        return (2 if f["marca"] else 0) + (1 if f["tipo_equipo"] else 0)

    validas = [(especificidad(f), f) for f in candidatas if especificidad(f) >= 0]
    if not validas:
        return GENERICOS
    mejor = max(validas, key=lambda x: x[0])[1]
    return [p.strip() for p in mejor["pasos_sugeridos"].split("|") if p.strip()]

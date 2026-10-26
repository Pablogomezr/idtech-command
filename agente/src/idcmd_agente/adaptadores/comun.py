"""Utilidades compartidas por los adaptadores de equipos con API HTTP."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_NO_PERMITIDO = re.compile(r"[^\w .\-+()/]")
_ZONA_POSIX = re.compile(r"([+-])(\d{1,2})(?::(\d{2}))?")


def texto_seguro(valor: str | None) -> str | None:
    """Ajusta un texto del fabricante al vocabulario común (caracteres seguros, máx. 64)."""
    if not valor:
        return None
    limpio = " ".join(_NO_PERMITIDO.sub(" ", valor).split())[:64]
    return limpio or None


def zona_posix(texto: str) -> timezone | None:
    """'CST+5:00:00' (notación POSIX: el signo va invertido) -> UTC-05:00."""
    m = _ZONA_POSIX.search(texto or "")
    if not m:
        return None
    signo = -1 if m.group(1) == "+" else 1
    return timezone(signo * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def a_utc(hora_equipo: datetime, zona_por_defecto: str) -> datetime:
    if hora_equipo.tzinfo is None:
        hora_equipo = hora_equipo.replace(tzinfo=ZoneInfo(zona_por_defecto))
    return hora_equipo.astimezone(UTC)


def desfase_segundos(hora_equipo_utc: datetime, ahora: datetime) -> float:
    """Positivo si el reloj del equipo va adelantado."""
    return round((hora_equipo_utc - ahora).total_seconds(), 1)

"""Agregados horarios: se guardan resúmenes, no cada sondeo crudo."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from .modelos import AgregadoHora, Estado, ResultadoChequeo

# Espacio de nombres fijo para los UUID deterministas de los agregados.
_NS_AGREGADOS = uuid.UUID("6f0c9a52-6c51-4b8e-9f43-2b1d4c7e8a10")

Clave = tuple[uuid.UUID, str, datetime]


def truncar_hora(momento: datetime) -> datetime:
    return momento.replace(minute=0, second=0, microsecond=0)


@dataclass
class _Acumulado:
    ok: int = 0
    degradado: int = 0
    caido: int = 0
    desconocido: int = 0
    latencia_suma: float = 0.0
    latencia_n: int = 0
    latencia_max: float | None = None


class Agregador:
    def __init__(self, sesion: uuid.UUID):
        # La sesión distingue las partes de una misma hora si el agente se reinicia.
        self._sesion = sesion
        self._acumulados: dict[Clave, _Acumulado] = {}

    def registrar(self, resultado: ResultadoChequeo) -> list[AgregadoHora]:
        """Suma la observación; devuelve las horas anteriores de esa serie ya cerradas."""
        hora = truncar_hora(resultado.observado_at)
        cerrados = [
            self._cerrar(clave)
            for clave in list(self._acumulados)
            if clave[:2] == (resultado.dispositivo_id, resultado.chequeo) and clave[2] < hora
        ]
        acum = self._acumulados.setdefault(
            (resultado.dispositivo_id, resultado.chequeo, hora), _Acumulado()
        )
        match resultado.estado:
            case Estado.OK:
                acum.ok += 1
            case Estado.DEGRADADO:
                acum.degradado += 1
            case Estado.CAIDO:
                acum.caido += 1
            case Estado.DESCONOCIDO:
                acum.desconocido += 1
        latencia = resultado.metricas.get("latencia_ms")
        if latencia is not None:
            acum.latencia_suma += latencia
            acum.latencia_n += 1
            acum.latencia_max = max(latencia, acum.latencia_max or latencia)
        return cerrados

    def cerrar_hasta(self, ahora: datetime) -> list[AgregadoHora]:
        """Cierra las horas ya terminadas, incluso de series que dejaron de reportar."""
        limite = truncar_hora(ahora)
        return [self._cerrar(c) for c in list(self._acumulados) if c[2] < limite]

    def vaciar(self) -> list[AgregadoHora]:
        """Cierra todo, incluida la hora en curso (al detener el agente)."""
        return [self._cerrar(c) for c in list(self._acumulados)]

    def _cerrar(self, clave: Clave) -> AgregadoHora:
        acum = self._acumulados.pop(clave)
        dispositivo_id, chequeo, hora = clave
        return AgregadoHora(
            id=uuid.uuid5(
                _NS_AGREGADOS, f"{dispositivo_id}|{chequeo}|{hora.isoformat()}|{self._sesion}"
            ),
            dispositivo_id=dispositivo_id,
            chequeo=chequeo,
            hora=hora,
            muestras_ok=acum.ok,
            muestras_degradado=acum.degradado,
            muestras_caido=acum.caido,
            muestras_desconocido=acum.desconocido,
            latencia_prom_ms=(
                round(acum.latencia_suma / acum.latencia_n, 2) if acum.latencia_n else None
            ),
            latencia_max_ms=acum.latencia_max,
        )

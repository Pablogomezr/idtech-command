"""Detección de cambios de estado con antirrebote.

Un estado nuevo solo se confirma tras N observaciones consecutivas iguales:

* hacia CAIDO, DEGRADADO o DESCONOCIDO: `confirmaciones_falla` (3 por defecto);
* hacia OK: `confirmaciones_recuperacion` (2), salvo desde DESCONOCIDO (1), para
  que el arranque del agente no demore en mostrar equipos sanos.

El evento lleva como `observado_at` la hora de la PRIMERA observación de la
racha: una caída empieza cuando el equipo dejó de responder, no cuando se
confirmó. Eso hace exacta la duración de las caídas para el SLA.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .modelos import Estado, EventoEstado, ResultadoChequeo

Clave = tuple[uuid.UUID, str]


@dataclass
class _Seguimiento:
    estado: Estado = Estado.DESCONOCIDO
    desde: datetime | None = None
    candidato: Estado | None = None
    racha: int = 0
    racha_desde: datetime | None = None


class MaquinaEstados:
    def __init__(self, confirmaciones_falla: int = 3, confirmaciones_recuperacion: int = 2):
        if confirmaciones_falla < 1 or confirmaciones_recuperacion < 1:
            raise ValueError("las confirmaciones deben ser >= 1")
        self.confirmaciones_falla = confirmaciones_falla
        self.confirmaciones_recuperacion = confirmaciones_recuperacion
        self._seguimiento: dict[Clave, _Seguimiento] = {}

    def cargar(self, estados: Iterable[tuple[uuid.UUID, str, Estado, datetime]]) -> None:
        """Restaura estados confirmados (tras un reinicio) para no emitir eventos espurios."""
        for dispositivo_id, chequeo, estado, desde in estados:
            self._seguimiento[(dispositivo_id, chequeo)] = _Seguimiento(estado, desde)

    def estado(self, dispositivo_id: uuid.UUID, chequeo: str) -> Estado:
        seg = self._seguimiento.get((dispositivo_id, chequeo))
        return seg.estado if seg else Estado.DESCONOCIDO

    def procesar(self, resultado: ResultadoChequeo) -> EventoEstado | None:
        seg = self._seguimiento.setdefault(
            (resultado.dispositivo_id, resultado.chequeo), _Seguimiento()
        )
        if resultado.estado == seg.estado:
            seg.candidato, seg.racha, seg.racha_desde = None, 0, None
            return None

        if resultado.estado == seg.candidato:
            seg.racha += 1
        else:
            seg.candidato, seg.racha, seg.racha_desde = resultado.estado, 1, resultado.observado_at

        if seg.racha < self._requeridas(seg.estado, resultado.estado):
            return None

        evento = EventoEstado(
            dispositivo_id=resultado.dispositivo_id,
            chequeo=resultado.chequeo,
            estado_anterior=seg.estado,
            estado_nuevo=resultado.estado,
            observado_at=seg.racha_desde,
            codigo=resultado.codigo,
            metricas=resultado.metricas,
        )
        seg.estado, seg.desde = resultado.estado, seg.racha_desde
        seg.candidato, seg.racha, seg.racha_desde = None, 0, None
        return evento

    def _requeridas(self, actual: Estado, nuevo: Estado) -> int:
        if nuevo == Estado.OK:
            return 1 if actual == Estado.DESCONOCIDO else self.confirmaciones_recuperacion
        return self.confirmaciones_falla

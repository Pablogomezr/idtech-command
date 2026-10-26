from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from idcmd_agente.nucleo.modelos import Estado, ResultadoChequeo

T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


class RelojFalso:
    def __init__(self, inicio: datetime = T0):
        self.ahora = inicio

    def __call__(self) -> datetime:
        return self.ahora

    def avanzar(self, segundos: float) -> None:
        self.ahora += timedelta(seconds=segundos)


@pytest.fixture
def reloj() -> RelojFalso:
    return RelojFalso()


@pytest.fixture
def dispositivo() -> uuid.UUID:
    return uuid.UUID("11111111-1111-4111-8111-111111111111")


def resultado(
    dispositivo_id: uuid.UUID,
    estado: Estado,
    momento: datetime,
    chequeo: str = "icmp",
    codigo: str | None = None,
    **metricas,
) -> ResultadoChequeo:
    return ResultadoChequeo(
        dispositivo_id=dispositivo_id,
        chequeo=chequeo,
        estado=estado,
        observado_at=momento,
        codigo=codigo,
        metricas=metricas,
    )

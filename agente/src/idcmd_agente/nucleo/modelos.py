"""Modelos del agente y del contrato de ingesta v1 (agente -> central).

Todo lo que sale del agente pasa por estos modelos. Dos reglas los gobiernan:

* `extra="forbid"`: un campo no declarado (por ejemplo, una credencial) no
  puede viajar por error.
* Vocabulario común: un adaptador describe la salud de un equipo solo con las
  métricas de `METRICAS_PERMITIDAS`. La central nunca conoce marcas.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION_CONTRATO = "1"

# Detección <= 5 min: intervalo * confirmaciones + margen de envío y refresco del portal.
PRESUPUESTO_DETECCION_S = 300
MARGEN_ENVIO_PORTAL_S = 60


class Estado(StrEnum):
    OK = "ok"
    DEGRADADO = "degradado"
    CAIDO = "caido"
    DESCONOCIDO = "desconocido"


# Vocabulario común: nombre de la métrica -> tipos aceptados.
METRICAS_PERMITIDAS: dict[str, tuple[type, ...]] = {
    "latencia_ms": (int, float),
    "perdida_pct": (int, float),
    "grabando": (bool,),
    "retencion_real_dias": (int, float),
    "hdd_estado": (str,),
    "firmware": (str,),
    "modelo": (str,),
    "desfase_reloj_s": (int, float),
}

PATRON_CODIGO = re.compile(r"^[a-z0-9_]{1,64}$")
_PATRON_TEXTO_METRICA = re.compile(r"^[\w .\-+()/]{1,64}$")


def ahora_utc() -> datetime:
    return datetime.now(UTC)


def validar_codigo(valor: str | None) -> str | None:
    if valor is not None and not PATRON_CODIGO.fullmatch(valor):
        raise ValueError(f"código inválido (solo [a-z0-9_], máx. 64): {valor!r}")
    return valor


def validar_metricas(metricas: dict[str, Any]) -> dict[str, Any]:
    for clave, valor in metricas.items():
        tipos = METRICAS_PERMITIDAS.get(clave)
        if tipos is None:
            raise ValueError(f"métrica fuera del vocabulario común: {clave!r}")
        # bool es subclase de int: no se acepta True como latencia.
        if isinstance(valor, bool) and bool not in tipos:
            raise ValueError(f"tipo inválido para {clave!r}")
        if not isinstance(valor, tipos):
            raise ValueError(f"tipo inválido para {clave!r}")
        if isinstance(valor, str) and not _PATRON_TEXTO_METRICA.fullmatch(valor):
            raise ValueError(f"texto inválido para {clave!r}")
    return metricas


def _a_utc(valor: datetime) -> datetime:
    if valor.tzinfo is None:
        raise ValueError("las fechas deben incluir zona horaria")
    return valor.astimezone(UTC)


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Objetivo(_Estricto):
    """Un equipo a vigilar con un adaptador. Viene del inventario; nunca trae secretos."""

    dispositivo_id: uuid.UUID
    adaptador: str
    direccion: str
    credencial_ref: str | None = None
    intervalo_s: int = Field(60, ge=10, le=900)
    parametros: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("adaptador")
    @classmethod
    def validar_adaptador(cls, v: str) -> str:
        return validar_codigo(v)


class ResultadoChequeo(_Estricto):
    """Una observación de un adaptador. Uso interno del agente; no se envía cruda."""

    dispositivo_id: uuid.UUID
    chequeo: str
    estado: Estado
    observado_at: datetime = Field(default_factory=ahora_utc)
    codigo: str | None = None
    metricas: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chequeo", "codigo")
    @classmethod
    def validar_codigos(cls, v: str | None) -> str | None:
        return validar_codigo(v)

    @field_validator("metricas")
    @classmethod
    def validar_vocabulario(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validar_metricas(v)

    @field_validator("observado_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)


class EventoEstado(_Estricto):
    """Cambio de estado confirmado. `observado_at` es el inicio de la racha que lo confirmó."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    dispositivo_id: uuid.UUID
    chequeo: str
    estado_anterior: Estado
    estado_nuevo: Estado
    observado_at: datetime
    codigo: str | None = None
    metricas: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chequeo", "codigo")
    @classmethod
    def validar_codigos(cls, v: str | None) -> str | None:
        return validar_codigo(v)

    @field_validator("metricas")
    @classmethod
    def validar_vocabulario(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validar_metricas(v)

    @field_validator("observado_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)


class AgregadoHora(_Estricto):
    """Resumen horario por dispositivo y chequeo.

    Una misma hora puede llegar en varias partes (una por sesión del agente si
    se reinicia); la central las suma. El `id` es determinista por parte, así
    que reenviar una parte es idempotente.
    """

    id: uuid.UUID
    dispositivo_id: uuid.UUID
    chequeo: str
    hora: datetime
    muestras_ok: int = Field(ge=0)
    muestras_degradado: int = Field(ge=0)
    muestras_caido: int = Field(ge=0)
    muestras_desconocido: int = Field(ge=0)
    latencia_prom_ms: float | None = None
    latencia_max_ms: float | None = None

    @field_validator("chequeo")
    @classmethod
    def validar_chequeo(cls, v: str) -> str:
        return validar_codigo(v)

    @field_validator("hora")
    @classmethod
    def validar_hora(cls, v: datetime) -> datetime:
        return _a_utc(v)


class Latido(_Estricto):
    agente_version: str
    reloj_agente: datetime
    inicio_sesion: datetime
    pendientes: int = Field(ge=0)
    adaptadores: list[str]

    @field_validator("reloj_agente", "inicio_sesion")
    @classmethod
    def validar_fechas(cls, v: datetime) -> datetime:
        return _a_utc(v)


class LoteIngesta(_Estricto):
    """Cuerpo de POST /api/v1/ingesta (gzip). Contrato v1."""

    version_contrato: Literal["1"] = VERSION_CONTRATO
    agente_id: uuid.UUID
    enviado_at: datetime
    latido: Latido
    eventos: list[EventoEstado] = Field(default_factory=list)
    agregados: list[AgregadoHora] = Field(default_factory=list)

    @field_validator("enviado_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)


class RespuestaIngesta(BaseModel):
    """Respuesta de la central. Ignora campos desconocidos para tolerar versiones nuevas."""

    model_config = ConfigDict(extra="ignore")

    aceptados: int = Field(ge=0)
    # Reservado para Fase 2 (reinicio remoto firmado). En v1 debe venir vacío.
    comandos: list[dict[str, Any]] = Field(default_factory=list)

"""Contrato de ingesta v1 del lado de la central.

Espejo de `agente/src/idcmd_agente/nucleo/modelos.py`. La fuente de verdad compartida es
`contrato/ingesta-v1.schema.json`: una prueba en cada lado verifica que sus modelos
produzcan ese mismo esquema, así que una divergencia rompe el CI y no el piloto.

La central valida por su cuenta: no confía en que el agente ya lo hizo.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION_CONTRATO = "1"


class Estado(StrEnum):
    OK = "ok"
    DEGRADADO = "degradado"
    CAIDO = "caido"
    DESCONOCIDO = "desconocido"


METRICAS_PERMITIDAS: dict[str, tuple[type, ...]] = {
    "latencia_ms": (int, float),
    "perdida_pct": (int, float),
    "grabando": (bool,),
    "retencion_real_dias": (int, float),
    "hdd_estado": (str,),
    "firmware": (str,),
    "modelo": (str,),
    "desfase_reloj_s": (int, float),
    "zona_horaria_utc_h": (int, float),  # zona configurada en el equipo (p. ej. -5)
    "ntp_activo": (bool,),
    "bloqueo_restante_s": (int,),  # el equipo bloqueó la cuenta por intentos fallidos
    "numero_serie": (str,),
    # Control de acceso: salud del panel, nunca eventos ni marcaciones de personas.
    "lectoras_desconectadas": (int,),
    "tamper_activo": (bool,),
    "bateria_baja": (bool,),
    # Almacenamiento de NVR/DVR.
    "discos_con_error": (int,),
    "almacenamiento_uso_pct": (int, float),
    "almacenamiento_total_gb": (int,),
    "almacenamiento_libre_gb": (int,),
    "discos_total": (int,),
    "retencion_proyectada_dias": (int, float),
    # Control de acceso: solo TOTALES y configuración de seguridad, nunca datos de personas
    # (ADR 0008).
    "usuarios_registrados": (int,),
    "tarjetas_registradas": (int,),
    "modos_verificacion": (str,),  # códigos tal como los reporta el panel, p. ej. "7,3,3,3"
}

# Mediciones que varían en cada chequeo: no cuentan como "cambio" para el historial de
# configuración (sí se actualizan en el valor actual).
METRICAS_VOLATILES = frozenset(
    {
        "latencia_ms",
        "perdida_pct",
        "desfase_reloj_s",
        "retencion_real_dias",
        "retencion_proyectada_dias",
        "almacenamiento_uso_pct",
        "almacenamiento_libre_gb",
        "bloqueo_restante_s",
    }
)

PATRON_CODIGO = re.compile(r"^[a-z0-9_]{1,64}$")
_PATRON_TEXTO_METRICA = re.compile(r"^[\w .\-+()/:,]{1,64}$")


def validar_codigo(valor: str | None) -> str | None:
    if valor is not None and not PATRON_CODIGO.fullmatch(valor):
        raise ValueError("código inválido")
    return valor


def validar_metricas(metricas: dict[str, Any]) -> dict[str, Any]:
    for clave, valor in metricas.items():
        tipos = METRICAS_PERMITIDAS.get(clave)
        if tipos is None:
            raise ValueError(f"métrica fuera del vocabulario común: {clave!r}")
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


class EventoEstado(_Estricto):
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


class Instantanea(_Estricto):
    id: uuid.UUID
    dispositivo_id: uuid.UUID
    chequeo: str
    estado: Estado
    observado_at: datetime
    metricas: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chequeo")
    @classmethod
    def validar_chequeo(cls, v: str) -> str:
        return validar_codigo(v)

    @field_validator("metricas")
    @classmethod
    def validar_vocabulario(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validar_metricas(v)

    @field_validator("observado_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)


TIPOS_OPERACION = frozenset(
    {
        "inicio_sesion",
        "inicio_sesion_fallido",
        "cierre_sesion",
        "cambio_configuracion",
        "busqueda_video",
        "reproduccion_video",
        "exportacion_video",
        "borrado_registros",
        "reinicio",
        "actualizacion_firmware",
        "otra",
    }
)


class OperacionEquipo(_Estricto):
    id: uuid.UUID
    dispositivo_id: uuid.UUID
    ocurrido_at: datetime
    tipo: str
    cuenta: str | None = None
    ip_origen: str | None = None
    codigo_fabricante: str | None = None

    @field_validator("tipo")
    @classmethod
    def validar_tipo(cls, v: str) -> str:
        if v not in TIPOS_OPERACION:
            raise ValueError(f"tipo de operación desconocido: {v!r}")
        return v

    @field_validator("cuenta", "codigo_fabricante")
    @classmethod
    def validar_texto(cls, v: str | None) -> str | None:
        if v is not None and not _PATRON_TEXTO_METRICA.fullmatch(v):
            raise ValueError("texto inválido")
        return v

    @field_validator("ip_origen")
    @classmethod
    def validar_ip(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_address(v)) if v is not None else None

    @field_validator("ocurrido_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)


class LoteIngesta(_Estricto):
    version_contrato: Literal["1"] = VERSION_CONTRATO
    agente_id: uuid.UUID
    enviado_at: datetime
    latido: Latido
    eventos: list[EventoEstado] = Field(default_factory=list)
    agregados: list[AgregadoHora] = Field(default_factory=list)
    instantaneas: list[Instantanea] = Field(default_factory=list)
    operaciones: list[OperacionEquipo] = Field(default_factory=list)

    @field_validator("enviado_at")
    @classmethod
    def validar_fecha(cls, v: datetime) -> datetime:
        return _a_utc(v)

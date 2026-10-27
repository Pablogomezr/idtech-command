"""Salud básica de equipos Dahua: controladoras de acceso (`dahua_acceso`) y cámaras
(`dahua_camara`). Lee modelo, firmware, número de serie y reloj.

Nunca consulta registros de acceso (marcaciones), usuarios ni tarjetas, y nunca abre
puertas ni reinicia (ver dahua_comun.py).

Parámetros del objetivo:
    esquema, verificar_tls   como en hikvision_acceso
    umbral_desfase_s         desfase de reloj tolerado (120 s)
    zona_horaria_equipo      la API devuelve hora local sin zona ("America/Bogota")

Limitación conocida (acceso): la API CGI no ofrece una consulta simple de tamper ni de
lectoras; se explorará con el equipo real sin usar flujos de eventos (traen marcaciones).

PENDIENTE DE VALIDAR con equipos reales (tests/fixtures/dahua_acceso/).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar
from .dahua_comun import OPS_IDENTIDAD, ErrorDahua, crear_cliente, leer_identidad

OPERACIONES = OPS_IDENTIDAD


class _AdaptadorDahuaBasico:
    chequeo: ClassVar[str]
    capacidades: ClassVar[frozenset[str]] = frozenset({"disponibilidad"})

    def __init__(
        self,
        cliente_http: httpx.AsyncClient | None = None,
        reloj: Callable[[], datetime] = ahora_utc,
        capturar_en: Path | None = None,
    ):
        self._cliente_http = cliente_http
        self._reloj = reloj
        self._capturar_en = capturar_en

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo]:
        def resultado(estado: Estado, codigo: str | None = None, **metricas) -> ResultadoChequeo:
            return ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo=self.chequeo,
                estado=estado,
                observado_at=self._reloj(),
                codigo=codigo,
                metricas=metricas,
            )

        if credencial is None:
            return [resultado(Estado.DESCONOCIDO, "credencial_requerida")]
        cliente = crear_cliente(
            objetivo, credencial, OPERACIONES, self._cliente_http, self._capturar_en
        )
        try:
            metricas, hallazgos = await leer_identidad(cliente, objetivo.parametros, self._reloj())
        except ErrorDahua as e:
            return [resultado(e.estado, e.codigo, **e.metricas)]
        except httpx.TransportError:
            return [resultado(Estado.CAIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()
        if hallazgos:
            return [resultado(Estado.DEGRADADO, hallazgos[0], **metricas)]
        return [resultado(Estado.OK, **metricas)]


@registrar("dahua_acceso")
class AdaptadorDahuaAcceso(_AdaptadorDahuaBasico):
    chequeo = "dahua_acceso"
    capacidades = frozenset({"disponibilidad", "salud_acceso"})


@registrar("dahua_camara")
class AdaptadorDahuaCamara(_AdaptadorDahuaBasico):
    chequeo = "dahua_camara"

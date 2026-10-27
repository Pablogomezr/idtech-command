"""Envío del buzón a la central: HTTPS saliente, lotes gzip, reintentos con espera.

* TLS obligatorio (solo se admite http:// contra localhost, para desarrollo).
* Respeta HTTPS_PROXY / NO_PROXY del entorno y admite una CA corporativa
  adicional (proxies con inspección TLS).
* 401/403 = token revocado: se deja de insistir rápido, pero se sigue
  recolectando en el buzón; nada se pierde.
* Cada envío incluye el latido, aunque no haya ítems: la ausencia de latidos
  es lo que le dice a la central "agente sin comunicación".
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import random
import ssl
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .buzon import Buzon
from .modelos import (
    AgregadoHora,
    EventoEstado,
    Instantanea,
    Latido,
    LoteIngesta,
    RespuestaIngesta,
    ahora_utc,
)

log = logging.getLogger(__name__)

RUTA_INGESTA = "/api/v1/ingesta"
_HOSTS_DESARROLLO = {"localhost", "127.0.0.1", "::1"}


class TokenRevocado(Exception):
    pass


class ErrorTemporal(Exception):
    pass


@dataclass(frozen=True)
class ConfigEnvio:
    url_central: str
    token: str
    agente_id: uuid.UUID
    ca_adicional: Path | None = None
    tamano_lote: int = 500
    timeout_s: float = 20.0
    espera_min_s: float = 2.0
    espera_max_s: float = 300.0
    intervalo_latido_s: float = 60.0
    espera_token_revocado_s: float = 900.0

    def __post_init__(self) -> None:
        partes = urlsplit(self.url_central)
        if partes.scheme != "https" and not (
            partes.scheme == "http" and partes.hostname in _HOSTS_DESARROLLO
        ):
            raise ValueError("la URL de la central debe usar https://")


def crear_cliente_http(config: ConfigEnvio) -> httpx.AsyncClient:
    contexto = ssl.create_default_context()
    if config.ca_adicional:
        contexto.load_verify_locations(cafile=str(config.ca_adicional))
    return httpx.AsyncClient(verify=contexto, trust_env=True, timeout=config.timeout_s)


class Enviador:
    def __init__(
        self,
        config: ConfigEnvio,
        buzon: Buzon,
        latido: Callable[[], Latido],
        cliente: httpx.AsyncClient | None = None,
        reloj: Callable[[], datetime] = ahora_utc,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config = config
        self.buzon = buzon
        self._latido = latido
        self._cliente = cliente or crear_cliente_http(config)
        self._reloj = reloj
        self._dormir = dormir
        self._lote = config.tamano_lote
        self._despertar = asyncio.Event()

    def despertar(self) -> None:
        """Pide un envío inmediato (p. ej. al confirmarse una caída)."""
        self._despertar.set()

    async def enviar_pendientes(self) -> int:
        """Envía todo lo pendiente (al menos un lote, con el latido). Devuelve ítems enviados."""
        enviados = 0
        while True:
            items = self.buzon.tomar_lote(self._lote)
            lote = LoteIngesta(
                agente_id=self.config.agente_id,
                enviado_at=self._reloj(),
                latido=self._latido(),
                eventos=[
                    EventoEstado.model_validate_json(i.cuerpo) for i in items if i.tipo == "evento"
                ],
                agregados=[
                    AgregadoHora.model_validate_json(i.cuerpo)
                    for i in items
                    if i.tipo == "agregado"
                ],
                instantaneas=[
                    Instantanea.model_validate_json(i.cuerpo)
                    for i in items
                    if i.tipo == "instantanea"
                ],
            )
            await self._publicar(lote)
            self.buzon.marcar_enviados([i.id for i in items], self._reloj())
            enviados += len(items)
            if len(items) < self._lote or self.buzon.pendientes() == 0:
                return enviados

    async def _publicar(self, lote: LoteIngesta) -> RespuestaIngesta:
        cuerpo = gzip.compress(lote.model_dump_json().encode())
        respuesta = await self._cliente.post(
            self.config.url_central.rstrip("/") + RUTA_INGESTA,
            content=cuerpo,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        if respuesta.status_code in (401, 403):
            raise TokenRevocado(f"la central rechazó el token ({respuesta.status_code})")
        if respuesta.status_code == 413:
            self._lote = max(1, self._lote // 2)
            raise ErrorTemporal(f"lote demasiado grande; se reduce a {self._lote}")
        if respuesta.status_code != 200:
            raise ErrorTemporal(f"respuesta inesperada de la central: {respuesta.status_code}")
        resultado = RespuestaIngesta.model_validate_json(respuesta.content)
        if resultado.comandos:
            log.warning("la central envió comandos; el contrato v1 no los ejecuta y se ignoran")
        return resultado

    async def ejecutar(self) -> None:
        espera_error = self.config.espera_min_s
        while True:
            try:
                await self.enviar_pendientes()
            except TokenRevocado as e:
                log.critical("%s; se sigue recolectando en el buzón local", e)
                await self._dormir(self.config.espera_token_revocado_s)
                continue
            except (httpx.TransportError, ErrorTemporal) as e:
                pendientes = self.buzon.pendientes()
                log.warning("sin envío (%s); %d ítems esperan en el buzón", e, pendientes)
                await self._dormir(espera_error * random.uniform(0.8, 1.2))
                espera_error = min(espera_error * 2, self.config.espera_max_s)
                continue
            espera_error = self.config.espera_min_s
            try:
                await asyncio.wait_for(self._despertar.wait(), self.config.intervalo_latido_s)
            except TimeoutError:
                pass
            self._despertar.clear()

    async def cerrar(self) -> None:
        await self._cliente.aclose()

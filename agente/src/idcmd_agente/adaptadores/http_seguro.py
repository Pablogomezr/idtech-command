"""Cliente HTTP de solo lectura para adaptadores (ISAPI, Dahua CGI, Milestone, BioStar 2...).

Cada adaptador declara la lista blanca exacta de operaciones que usa. Cualquier otra
llamada lanza `OperacionNoPermitida` antes de salir a la red. Así el principio "solo
lectura sobre los equipos del cliente" es verificable con pruebas.

La lista blanca cubre también los PARÁMETROS de la consulta, no solo la ruta: en la API
CGI de Dahua la misma ruta lee o actúa según `action` (`magicBox.cgi?action=getDeviceType`
lee; `magicBox.cgi?action=reboot` reinicia; `accessControl.cgi?action=openDoor` abre la
puerta). Por eso:

* la consulta solo se pasa en `params`, nunca escrita dentro de la ruta;
* los nombres de parámetros deben ser exactamente los declarados (ni uno más);
* cada valor debe cumplir su expresión regular.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

# `content`: solo para búsquedas que se piden con cuerpo XML (Hikvision logSearch); la
# operación (método y ruta) igual tiene que estar en la lista blanca.
_ARGUMENTOS_PERMITIDOS = {"params", "headers", "json", "content", "timeout"}


class OperacionNoPermitida(Exception):
    pass


@dataclass(frozen=True)
class Operacion:
    """Una operación de lectura permitida. `ruta` y cada valor de `parametros` son
    expresiones regulares que deben cubrir el texto completo."""

    metodo: str
    ruta: str
    parametros: Mapping[str, str] = field(default_factory=dict)

    def admite(self, metodo: str, ruta: str, params: Mapping[str, Any]) -> bool:
        if metodo.upper() != self.metodo.upper() or not re.fullmatch(self.ruta, ruta):
            return False
        if set(params) != set(self.parametros):
            return False
        return all(re.fullmatch(self.parametros[k], str(v)) for k, v in params.items())


class ClienteSoloLectura:
    def __init__(
        self,
        url_base: str,
        permitidas: Iterable[Operacion | tuple[str, str]],
        cliente: httpx.AsyncClient | None = None,
        auth: httpx.Auth | None = None,
        verificar_tls: bool = True,
        capturar_en: Path | None = None,
    ):
        """`verificar_tls=False` solo para equipos de la LAN con certificado autofirmado
        (lo habitual en paneles y NVR); la conexión a la central siempre se verifica.
        `capturar_en`: guarda el cuerpo de cada respuesta (solo para desarrollar
        adaptadores con equipos reales; nunca en producción)."""
        self._url_base = url_base.rstrip("/")
        self._permitidas = [p if isinstance(p, Operacion) else Operacion(*p) for p in permitidas]
        self._cliente = cliente or httpx.AsyncClient(timeout=10.0, verify=verificar_tls)
        self._auth = auth
        self._capturar_en = capturar_en
        self._capturas = 0

    def permitida(self, metodo: str, ruta: str, params: Mapping[str, Any] | None = None) -> bool:
        if any(c in ruta for c in "?#") or ".." in ruta:
            return False
        return any(op.admite(metodo, ruta, params or {}) for op in self._permitidas)

    async def solicitar(self, metodo: str, ruta: str, **kwargs: Any) -> httpx.Response:
        extra = set(kwargs) - _ARGUMENTOS_PERMITIDOS
        if extra:
            raise OperacionNoPermitida(f"argumentos no permitidos: {sorted(extra)}")
        params = kwargs.get("params") or {}
        if not self.permitida(metodo, ruta, params):
            raise OperacionNoPermitida(f"{metodo.upper()} {ruta} {dict(params)} no está permitida")
        # La consulta se arma aquí y conserva "[]" y ":": algunos firmwares (Dahua) rechazan
        # `condition.Types%5B0%5D`. Se valida el diccionario y se envía exactamente ese.
        url = self._url_base + ruta
        if params:
            url += "?" + urlencode(params, quote_via=quote, safe="[]:")
        kwargs.pop("params", None)
        respuesta = await self._cliente.request(
            metodo, url, auth=self._auth or httpx.USE_CLIENT_DEFAULT, **kwargs
        )
        if self._capturar_en is not None:
            self._capturar(ruta, params, respuesta)
        return respuesta

    def _capturar(self, ruta: str, params: Mapping[str, Any], respuesta: httpx.Response) -> None:
        self._capturas += 1
        nombre = re.sub(r"[^A-Za-z0-9]+", "_", ruta + "_" + "_".join(map(str, params.values())))
        self._capturar_en.mkdir(parents=True, exist_ok=True)
        destino = self._capturar_en / f"{self._capturas:02d}{nombre}.{respuesta.status_code}.txt"
        destino.write_bytes(respuesta.content)

    async def cerrar(self) -> None:
        await self._cliente.aclose()

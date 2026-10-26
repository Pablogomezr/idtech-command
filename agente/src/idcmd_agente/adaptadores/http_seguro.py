"""Cliente HTTP de solo lectura para adaptadores REST (Milestone, BioStar 2, ISAPI...).

Cada adaptador declara la lista blanca exacta de operaciones que usa. Cualquier
otra llamada lanza `OperacionNoPermitida` antes de salir a la red. Así el
principio "solo lectura sobre los equipos del cliente" es verificable con
pruebas, y en BioStar 2 queda imposible leer marcaciones por error.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import httpx


class OperacionNoPermitida(Exception):
    pass


class ClienteSoloLectura:
    def __init__(
        self,
        url_base: str,
        permitidas: Iterable[tuple[str, str]],
        cliente: httpx.AsyncClient | None = None,
    ):
        """`permitidas`: pares (MÉTODO, expresión regular que debe cubrir la ruta completa)."""
        self._url_base = url_base.rstrip("/")
        self._permitidas = [(m.upper(), re.compile(p)) for m, p in permitidas]
        self._cliente = cliente or httpx.AsyncClient(timeout=15.0)

    def permitida(self, metodo: str, ruta: str) -> bool:
        return any(metodo.upper() == m and patron.fullmatch(ruta) for m, patron in self._permitidas)

    async def solicitar(self, metodo: str, ruta: str, **kwargs: Any) -> httpx.Response:
        if not self.permitida(metodo, ruta):
            raise OperacionNoPermitida(f"{metodo.upper()} {ruta} no está en la lista blanca")
        return await self._cliente.request(metodo, self._url_base + ruta, **kwargs)

    async def cerrar(self) -> None:
        await self._cliente.aclose()

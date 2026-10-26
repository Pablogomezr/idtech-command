"""Central de pruebas: recibe lotes por respx y permite simular cortes de internet."""

from __future__ import annotations

import gzip

import httpx

from idcmd_agente.nucleo.modelos import LoteIngesta

URL = "https://central.prueba"


class CentralFalsa:
    def __init__(self):
        self.en_linea = True
        self.estado_http = 200
        self.lotes: list[LoteIngesta] = []
        self.cabeceras: list[httpx.Headers] = []

    def __call__(self, solicitud: httpx.Request) -> httpx.Response:
        if not self.en_linea:
            raise httpx.ConnectError("sin internet", request=solicitud)
        self.cabeceras.append(solicitud.headers)
        if self.estado_http != 200:
            return httpx.Response(self.estado_http)
        lote = LoteIngesta.model_validate_json(gzip.decompress(solicitud.content))
        self.lotes.append(lote)
        return httpx.Response(200, json={"aceptados": len(lote.eventos) + len(lote.agregados)})

    @property
    def eventos(self):
        return [e for lote in self.lotes for e in lote.eventos]

    @property
    def agregados(self):
        return [a for lote in self.lotes for a in lote.agregados]

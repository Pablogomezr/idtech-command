"""Controladoras y terminales de control de acceso Dahua por API HTTP (CGI), sin la nube DoLynk.

Solo lee la SALUD del equipo: modelo, firmware, número de serie y reloj. Nunca consulta
registros de acceso (marcaciones), usuarios ni tarjetas, y nunca abre puertas ni reinicia.

Cuidado especial: en esta API la misma ruta lee o actúa según el parámetro `action`
(`magicBox.cgi?action=reboot` reinicia, `accessControl.cgi?action=openDoor` abre la puerta).
La lista blanca fija cada `action` permitida y una prueba verifica las prohibidas.

Parámetros del objetivo:
    esquema, verificar_tls, umbral_desfase_s   como en hikvision_acceso
    zona_horaria_equipo   la API devuelve hora local sin zona ("America/Bogota")

Limitación conocida: la API CGI no ofrece una consulta simple de tamper ni de lectoras;
se explorará con el equipo real (sin usar flujos de eventos, que incluyen marcaciones).

PENDIENTE DE VALIDAR con un equipo real de la oficina (tests/fixtures/dahua_acceso/).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar
from .comun import a_utc, desfase_segundos, texto_seguro
from .http_seguro import ClienteSoloLectura, Operacion

CHEQUEO = "dahua_acceso"

OPERACIONES = [
    Operacion(
        "GET",
        r"/cgi-bin/magicBox\.cgi",
        {"action": r"getDeviceType|getSoftwareVersion|getSerialNo"},
    ),
    Operacion("GET", r"/cgi-bin/global\.cgi", {"action": r"getCurrentTime"}),
]


def _clave_valor(texto: str) -> dict[str, str]:
    """'type=ASC1204C-D\\r\\n' -> {'type': 'ASC1204C-D'}."""
    pares = (linea.split("=", 1) for linea in texto.splitlines() if "=" in linea)
    return {k.strip(): v.strip() for k, v in pares}


@registrar("dahua_acceso")
class AdaptadorDahuaAcceso:
    nombre: ClassVar[str]
    capacidades: ClassVar[frozenset[str]] = frozenset({"disponibilidad", "salud_acceso"})

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
        p = objetivo.parametros

        def resultado(estado: Estado, codigo: str | None = None, **metricas) -> ResultadoChequeo:
            return ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo=CHEQUEO,
                estado=estado,
                observado_at=self._reloj(),
                codigo=codigo,
                metricas={k: v for k, v in metricas.items() if v is not None},
            )

        if credencial is None:
            return [resultado(Estado.DESCONOCIDO, "credencial_requerida")]

        cliente = ClienteSoloLectura(
            f"{p.get('esquema', 'http')}://{objetivo.direccion}",
            OPERACIONES,
            cliente=self._cliente_http,
            auth=httpx.DigestAuth(credencial.usuario, credencial.clave.get_secret_value()),
            verificar_tls=bool(p.get("verificar_tls", False)),
            capturar_en=self._capturar_en,
        )

        async def magic(accion: str) -> httpx.Response:
            return await cliente.solicitar(
                "GET", "/cgi-bin/magicBox.cgi", params={"action": accion}
            )

        try:
            inicio = time.monotonic()
            r_tipo = await magic("getDeviceType")
            latencia = round((time.monotonic() - inicio) * 1000, 2)
            if r_tipo.status_code == 401:
                return [resultado(Estado.DESCONOCIDO, "credencial_rechazada")]
            if r_tipo.status_code != 200:
                return [resultado(Estado.DESCONOCIDO, "respuesta_no_reconocida")]
            r_version = await magic("getSoftwareVersion")
            r_serie = await magic("getSerialNo")
            r_hora = await cliente.solicitar(
                "GET", "/cgi-bin/global.cgi", params={"action": "getCurrentTime"}
            )
        except httpx.TransportError:
            return [resultado(Estado.CAIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        def campo(respuesta: httpx.Response, clave: str) -> str | None:
            if respuesta.status_code != 200:
                return None
            return _clave_valor(respuesta.text).get(clave)

        version = (campo(r_version, "version") or "").replace(",build:", " build ")
        metricas = {
            "latencia_ms": latencia,
            "modelo": texto_seguro(campo(r_tipo, "type")),
            "firmware": texto_seguro(version),
            "numero_serie": texto_seguro(campo(r_serie, "sn")),
        }

        hora_texto = campo(r_hora, "result")
        if hora_texto:
            try:
                hora = datetime.strptime(hora_texto, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                hora = None
            if hora is not None:
                zona = str(p.get("zona_horaria_equipo", "America/Bogota"))
                desfase = desfase_segundos(a_utc(hora, zona), self._reloj())
                metricas["desfase_reloj_s"] = desfase
                if abs(desfase) > float(p.get("umbral_desfase_s", 120)):
                    return [resultado(Estado.DEGRADADO, "reloj_desfasado", **metricas)]
        return [resultado(Estado.OK, **metricas)]

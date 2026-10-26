"""Base común de los adaptadores Dahua (API HTTP/CGI), sin la nube DoLynk.

Cuidado especial en esta API: la misma ruta lee o actúa según `action`
(`magicBox.cgi?action=reboot` reinicia, `accessControl.cgi?action=openDoor` abre la puerta)
y `configManager.cgi?action=getConfig&name=RemoteDevice` devuelve las CONTRASEÑAS de las
cámaras en texto plano. Por eso ningún adaptador Dahua usa `configManager.cgi`, y cada
`action` permitida está fijada en la lista blanca.

Las respuestas de Dahua cambian entre firmwares y la documentación pública es incompleta:
todo se interpreta de forma defensiva y se valida con `idcmd sondear --capturar`.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo
from .comun import a_utc, desfase_segundos, texto_seguro
from .http_seguro import ClienteSoloLectura, Operacion

FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"
PATRON_FECHA = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"

OPS_IDENTIDAD = [
    Operacion(
        "GET",
        r"/cgi-bin/magicBox\.cgi",
        {"action": r"getDeviceType|getSoftwareVersion|getSerialNo"},
    ),
    Operacion("GET", r"/cgi-bin/global\.cgi", {"action": r"getCurrentTime"}),
]


class ErrorDahua(Exception):
    def __init__(self, estado: Estado, codigo: str):
        super().__init__(codigo)
        self.estado, self.codigo = estado, codigo


def clave_valor(texto: str) -> dict[str, str]:
    """'type=ASC1204C-D\\r\\n' -> {'type': 'ASC1204C-D'}."""
    pares = (linea.split("=", 1) for linea in texto.splitlines() if "=" in linea)
    return {k.strip(): v.strip() for k, v in pares}


def crear_cliente(
    objetivo: Objetivo,
    credencial: Credencial,
    operaciones: list[Operacion],
    cliente_http: httpx.AsyncClient | None,
    capturar_en: Path | None,
) -> ClienteSoloLectura:
    p = objetivo.parametros
    return ClienteSoloLectura(
        f"{p.get('esquema', 'http')}://{objetivo.direccion}",
        operaciones,
        cliente=cliente_http,
        auth=httpx.DigestAuth(credencial.usuario, credencial.clave.get_secret_value()),
        verificar_tls=bool(p.get("verificar_tls", False)),
        capturar_en=capturar_en,
    )


def hora_local_a_utc(texto: str, zona: str) -> datetime | None:
    try:
        return a_utc(datetime.strptime(texto, FORMATO_FECHA), zona)
    except ValueError:
        return None


async def leer_identidad(
    cliente: ClienteSoloLectura, parametros: dict, ahora: datetime
) -> tuple[dict, list[str]]:
    """Modelo, firmware, serie, latencia y desfase de reloj. Devuelve (métricas, hallazgos).

    Lanza ErrorDahua si la credencial es rechazada o la respuesta no se reconoce;
    deja pasar httpx.TransportError (el equipo no responde).
    """

    async def magic(accion: str) -> httpx.Response:
        return await cliente.solicitar("GET", "/cgi-bin/magicBox.cgi", params={"action": accion})

    inicio = time.monotonic()
    r_tipo = await magic("getDeviceType")
    latencia = round((time.monotonic() - inicio) * 1000, 2)
    if r_tipo.status_code == 401:
        raise ErrorDahua(Estado.DESCONOCIDO, "credencial_rechazada")
    if r_tipo.status_code != 200:
        raise ErrorDahua(Estado.DESCONOCIDO, "respuesta_no_reconocida")
    r_version = await magic("getSoftwareVersion")
    r_serie = await magic("getSerialNo")
    r_hora = await cliente.solicitar(
        "GET", "/cgi-bin/global.cgi", params={"action": "getCurrentTime"}
    )

    def campo(respuesta: httpx.Response, clave: str) -> str | None:
        return clave_valor(respuesta.text).get(clave) if respuesta.status_code == 200 else None

    version = (campo(r_version, "version") or "").replace(",build:", " build ")
    metricas = {
        "latencia_ms": latencia,
        "modelo": texto_seguro(campo(r_tipo, "type")),
        "firmware": texto_seguro(version),
        "numero_serie": texto_seguro(campo(r_serie, "sn")),
    }
    hallazgos: list[str] = []
    hora_texto = campo(r_hora, "result")
    zona = str(parametros.get("zona_horaria_equipo", "America/Bogota"))
    if hora_texto and (hora := hora_local_a_utc(hora_texto, zona)):
        metricas["desfase_reloj_s"] = desfase_segundos(hora, ahora)
        if abs(metricas["desfase_reloj_s"]) > float(parametros.get("umbral_desfase_s", 120)):
            hallazgos.append("reloj_desfasado")
    return {k: v for k, v in metricas.items() if v is not None}, hallazgos

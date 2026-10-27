"""Paneles y terminales de control de acceso Hikvision por ISAPI (DS-K26xx, DS-K1Txxx).

Solo lee la SALUD del panel: modelo, firmware, número de serie, reloj, tamper de la caja,
lectoras en línea y batería. Nunca consulta eventos de acceso (marcaciones), usuarios,
tarjetas ni rostros, y nunca abre puertas ni reinicia: esas rutas no están en la lista
blanca y una prueba lo verifica.

Parámetros del objetivo:
    esquema               "http" (por defecto) o "https"
    verificar_tls         False por defecto (certificado autofirmado del equipo)
    lectoras_esperadas    lectoras conectadas que debe tener el panel; sin este dato no se
                          evalúan (los puertos libres también se reportan como "fuera de línea")
    umbral_desfase_s      desfase de reloj tolerado (120 s por defecto)
    zona_horaria_equipo   zona donde está instalado el equipo ("America/Bogota"); si el
                          equipo tiene otra configurada se informa zona_horaria_incorrecta

PENDIENTE DE VALIDAR con un equipo real de la oficina: los nombres de campos de
AcsWorkStatus varían entre firmwares. Las pruebas usan respuestas armadas desde la
documentación pública (tests/fixtures/hikvision_acceso/).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar
from .comun import texto_seguro, zona_posix
from .http_seguro import ClienteSoloLectura, Operacion

CHEQUEO = "hikvision_acceso"

OPERACIONES = [
    Operacion("GET", r"/ISAPI/System/deviceInfo"),
    Operacion("GET", r"/ISAPI/System/time"),
    Operacion("GET", r"/ISAPI/AccessControl/AcsWorkStatus", {"format": "json"}),
]


def _xml(texto: bytes) -> dict[str, str]:
    """Hojas de un XML de ISAPI sin espacio de nombres: {'model': 'DS-K2604T', ...}."""
    raiz = ET.fromstring(texto)
    return {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in raiz.iter()}


@registrar("hikvision_acceso")
class AdaptadorHikvisionAcceso:
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
        try:
            inicio = time.monotonic()
            r_info = await cliente.solicitar("GET", "/ISAPI/System/deviceInfo")
            latencia = round((time.monotonic() - inicio) * 1000, 2)
            if r_info.status_code == 401:
                return [resultado(Estado.DESCONOCIDO, "credencial_rechazada")]
            if r_info.status_code != 200:
                return [resultado(Estado.DESCONOCIDO, "respuesta_no_reconocida")]
            r_hora = await cliente.solicitar("GET", "/ISAPI/System/time")
            r_estado = await cliente.solicitar(
                "GET", "/ISAPI/AccessControl/AcsWorkStatus", params={"format": "json"}
            )
        except httpx.TransportError:
            return [resultado(Estado.CAIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        try:
            info = _xml(r_info.content)
        except ET.ParseError:
            return [resultado(Estado.DESCONOCIDO, "respuesta_no_reconocida")]

        metricas: dict[str, Any] = {
            "latencia_ms": latencia,
            "modelo": texto_seguro(info.get("model")),
            "firmware": texto_seguro(
                f"{info.get('firmwareVersion', '')} {info.get('firmwareReleasedDate', '')}"
            ),
            "numero_serie": texto_seguro(info.get("serialNumber")),
        }
        hallazgos: list[str] = []

        hora = self._hora(r_hora, p)
        if hora is not None:
            desfase, zona_h, zona_esperada_h, ntp = hora
            metricas["desfase_reloj_s"] = desfase
            metricas["ntp_activo"] = ntp
            if zona_h is not None:
                metricas["zona_horaria_utc_h"] = zona_h
                if zona_h != zona_esperada_h:
                    hallazgos.append("zona_horaria_incorrecta")
            if abs(desfase) > float(p.get("umbral_desfase_s", 120)):
                hallazgos.append("reloj_desfasado")

        estado_acs = self._estado_acceso(r_estado)
        if estado_acs is not None:
            if estado_acs.get("hostAntiDismantleStatus") == "open":
                metricas["tamper_activo"] = True
                hallazgos.append("tamper_activo")
            else:
                metricas["tamper_activo"] = False
            if "batteryLowVoltage" in estado_acs:
                metricas["bateria_baja"] = bool(estado_acs["batteryLowVoltage"])
                if metricas["bateria_baja"]:
                    hallazgos.append("bateria_baja")
            esperadas = p.get("lectoras_esperadas")
            en_linea = estado_acs.get("cardReaderOnlineStatus")
            if esperadas is not None and isinstance(en_linea, list):
                faltantes = max(0, int(esperadas) - sum(1 for x in en_linea if x == 1))
                metricas["lectoras_desconectadas"] = faltantes
                if faltantes:
                    hallazgos.append("lectora_desconectada")

        if hallazgos:
            # Un solo código por resultado: el más grave según este orden.
            orden = [
                "tamper_activo",
                "lectora_desconectada",
                "bateria_baja",
                "zona_horaria_incorrecta",
                "reloj_desfasado",
            ]
            return [resultado(Estado.DEGRADADO, min(hallazgos, key=orden.index), **metricas)]
        return [resultado(Estado.OK, **metricas)]

    def _hora(self, respuesta: httpx.Response, p: dict):
        """(desfase del reloj de pared en s, zona del equipo en h, zona esperada en h, NTP).

        Se separan dos fallas distintas: el reloj de pared (la hora que marca el equipo
        frente a la hora local real) y la zona horaria configurada. Un panel en Bogotá que
        marca la hora correcta pero tiene zona UTC+0 registra sus eventos 5 h corridos al
        integrarse con otros sistemas, aunque en su pantalla todo se vea bien.
        """
        if respuesta.status_code != 200:
            return None
        try:
            datos = _xml(respuesta.content)
            hora = datetime.fromisoformat(datos["localTime"])
        except (ET.ParseError, KeyError, ValueError):
            return None
        zona = hora.utcoffset() if hora.tzinfo else None
        if zona is None and (posix := zona_posix(datos.get("timeZone", ""))):
            zona = posix.utcoffset(None)
        local_esperada = self._reloj().astimezone(
            ZoneInfo(str(p.get("zona_horaria_equipo", "America/Bogota")))
        )
        desfase = round(
            (hora.replace(tzinfo=None) - local_esperada.replace(tzinfo=None)).total_seconds(), 1
        )
        zona_esperada_h = local_esperada.utcoffset().total_seconds() / 3600
        zona_h = zona.total_seconds() / 3600 if zona is not None else None
        return desfase, zona_h, zona_esperada_h, datos.get("timeMode", "").upper() == "NTP"

    @staticmethod
    def _estado_acceso(respuesta: httpx.Response) -> dict | None:
        # Algunos terminales no exponen este recurso: no es una falla del equipo.
        if respuesta.status_code != 200:
            return None
        try:
            return respuesta.json().get("AcsWorkStatus")
        except ValueError:
            return None

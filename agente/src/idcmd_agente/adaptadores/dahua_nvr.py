"""NVR Dahua: salud del grabador (`dahua_nvr`) y retención real por cámara (`dahua_retencion`).

`dahua_nvr` (cada 60 s, cuenta para la detección):
    * identidad del NVR: modelo, firmware, serie, reloj;
    * discos: con error, sin disco, porcentaje de uso;
    * por canal: si el NVR ve la cámara conectada (resultado a nombre de la cámara).

`dahua_retencion` (cada 15 min, `deteccion: false`):
    * por canal: fecha de la grabación más antigua -> días reales de retención, y
      comparación con `retencion_contratada_dias` si se indica.

Ambos son "de sistema": el objetivo trae `canales: {canal: dispositivo_id_de_la_camara}`.
Solo leen: nada de video en vivo, reproducción, descarga ni configuración. En particular no
usan `configManager.cgi` (RemoteDevice devuelve las contraseñas de las cámaras).

Parámetros:
    canal_base              numeración de canales del NVR en la API (1 por defecto)
    retencion_contratada_dias
    esquema, verificar_tls, umbral_desfase_s, zona_horaria_equipo   como en dahua_acceso

PENDIENTE DE VALIDAR con el NVR de la oficina: formatos de getDeviceAllInfo,
getCameraState y mediaFileFind varían por firmware (tests/fixtures/dahua_nvr/).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar
from .dahua_comun import (
    FORMATO_FECHA,
    OPS_IDENTIDAD,
    PATRON_FECHA,
    ErrorDahua,
    clave_valor,
    crear_cliente,
    hora_local_a_utc,
    leer_identidad,
)
from .http_seguro import ClienteSoloLectura, Operacion

OPS_NVR = [
    *OPS_IDENTIDAD,
    Operacion("GET", r"/cgi-bin/storageDevice\.cgi", {"action": r"getDeviceAllInfo"}),
    Operacion("POST", r"/cgi-bin/api/LogicDeviceManager/getCameraState"),
]

_OBJETO = r"\d{1,12}"
OPS_RETENCION = [
    Operacion("GET", r"/cgi-bin/mediaFileFind\.cgi", {"action": r"factory\.create"}),
    Operacion(
        "GET",
        r"/cgi-bin/mediaFileFind\.cgi",
        {
            "action": r"findFile",
            "object": _OBJETO,
            "condition.Channel": r"\d{1,3}",
            "condition.StartTime": PATRON_FECHA,
            "condition.EndTime": PATRON_FECHA,
            "condition.Types[0]": r"dav",
        },
    ),
    Operacion(
        "GET",
        r"/cgi-bin/mediaFileFind\.cgi",
        {"action": r"findNextFile", "object": _OBJETO, "count": r"1"},
    ),
    Operacion(
        "GET", r"/cgi-bin/mediaFileFind\.cgi", {"action": r"close|destroy", "object": _OBJETO}
    ),
]

_DISCO = re.compile(r"^list\.info\[(\d+)\]\.(.+)$")
_ESTADOS_ERROR = {"error", "abnormal", "fault", "unformatted"}


def interpretar_discos(texto: str) -> list[dict]:
    """Agrupa 'list.info[N].…' por disco: estado, error, bytes totales y usados."""
    discos: dict[int, dict] = {}
    for clave, valor in clave_valor(texto).items():
        m = _DISCO.match(clave)
        if not m:
            continue
        d = discos.setdefault(
            int(m.group(1)), {"estado": None, "error": False, "total": 0.0, "usado": 0.0}
        )
        campo = m.group(2)
        if campo == "State":
            d["estado"] = valor
        elif campo.endswith("IsError") and valor.lower() == "true":
            d["error"] = True
        elif campo.endswith("TotalBytes"):
            d["total"] += float(valor or 0)
        elif campo.endswith("UsedBytes"):
            d["usado"] += float(valor or 0)
    for d in discos.values():
        if (d["estado"] or "").lower() in _ESTADOS_ERROR:
            d["error"] = True
    return [discos[i] for i in sorted(discos)]


class _BaseNvr:
    capacidades: ClassVar[frozenset[str]]

    def __init__(
        self,
        cliente_http: httpx.AsyncClient | None = None,
        reloj: Callable[[], datetime] = ahora_utc,
        capturar_en: Path | None = None,
    ):
        self._cliente_http = cliente_http
        self._reloj = reloj
        self._capturar_en = capturar_en


@registrar("dahua_nvr")
class AdaptadorDahuaNvr(_BaseNvr):
    capacidades = frozenset({"disponibilidad", "almacenamiento", "canales"})

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo]:
        ahora = self._reloj()

        def resultado(dispositivo, chequeo, estado, codigo=None, **metricas):
            return ResultadoChequeo(
                dispositivo_id=dispositivo,
                chequeo=chequeo,
                estado=estado,
                observado_at=ahora,
                codigo=codigo,
                metricas=metricas,
            )

        nvr = objetivo.dispositivo_id
        if credencial is None:
            return [resultado(nvr, "dahua_nvr", Estado.DESCONOCIDO, "credencial_requerida")]
        cliente = crear_cliente(
            objetivo, credencial, OPS_NVR, self._cliente_http, self._capturar_en
        )
        try:
            metricas, hallazgos = await leer_identidad(cliente, objetivo.parametros, ahora)
            r_discos = await cliente.solicitar(
                "GET", "/cgi-bin/storageDevice.cgi", params={"action": "getDeviceAllInfo"}
            )
            r_camaras = None
            if objetivo.canales:
                r_camaras = await cliente.solicitar(
                    "POST",
                    "/cgi-bin/api/LogicDeviceManager/getCameraState",
                    json={"uniqueChannels": [-1]},
                )
        except ErrorDahua as e:
            return [resultado(nvr, "dahua_nvr", e.estado, e.codigo)]
        except httpx.TransportError:
            return [resultado(nvr, "dahua_nvr", Estado.CAIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        if r_discos.status_code == 200:
            discos = interpretar_discos(r_discos.text)
            con_error = sum(d["error"] for d in discos)
            total = sum(d["total"] for d in discos)
            metricas["discos_con_error"] = con_error
            if total:
                metricas["almacenamiento_uso_pct"] = round(
                    100 * sum(d["usado"] for d in discos) / total, 1
                )
            if not discos:
                metricas["hdd_estado"] = "sin_disco"
                hallazgos.insert(0, "sin_disco")
            elif con_error:
                metricas["hdd_estado"] = "error"
                hallazgos.insert(0, "disco_con_error")
            else:
                metricas["hdd_estado"] = "normal"

        resultados = [
            resultado(nvr, "dahua_nvr", Estado.DEGRADADO, hallazgos[0], **metricas)
            if hallazgos
            else resultado(nvr, "dahua_nvr", Estado.OK, **metricas)
        ]
        resultados += self._estado_camaras(r_camaras, objetivo, resultado)
        return resultados

    @staticmethod
    def _estado_camaras(respuesta, objetivo, resultado) -> list[ResultadoChequeo]:
        # Si el firmware no ofrece esta consulta, las cámaras se vigilan con ping directo.
        if respuesta is None or respuesta.status_code != 200:
            return []
        try:
            estados = {
                int(e["channel"]): str(e.get("connectionState", ""))
                for e in respuesta.json().get("states", [])
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            return []
        base = int(objetivo.parametros.get("canal_base", 1))
        salida = []
        for canal, camara in sorted(objetivo.canales.items()):
            estado = estados.get(canal - base)
            if estado is None:
                continue
            if estado.lower() == "connected":
                salida.append(resultado(camara, "dahua_nvr_canal", Estado.OK))
            else:
                salida.append(
                    resultado(camara, "dahua_nvr_canal", Estado.CAIDO, "camara_desconectada_nvr")
                )
        return salida


@registrar("dahua_retencion")
class AdaptadorDahuaRetencion(_BaseNvr):
    capacidades = frozenset({"retencion"})

    async def chequear(
        self, objetivo: Objetivo, credencial: Credencial | None
    ) -> list[ResultadoChequeo]:
        ahora = self._reloj()
        p = objetivo.parametros
        zona = str(p.get("zona_horaria_equipo", "America/Bogota"))
        contratada = p.get("retencion_contratada_dias")

        def resultado(camara, estado, codigo=None, **metricas):
            return ResultadoChequeo(
                dispositivo_id=camara,
                chequeo="dahua_retencion",
                estado=estado,
                observado_at=ahora,
                codigo=codigo,
                metricas=metricas,
            )

        if credencial is None:
            return [
                resultado(c, Estado.DESCONOCIDO, "credencial_requerida")
                for c in objetivo.canales.values()
            ]
        cliente = crear_cliente(
            objetivo, credencial, OPS_RETENCION, self._cliente_http, self._capturar_en
        )
        salida = []
        try:
            for canal, camara in sorted(objetivo.canales.items()):
                try:
                    inicio = await self._grabacion_mas_antigua(cliente, canal, ahora, zona)
                except ErrorDahua as e:
                    salida.append(resultado(camara, e.estado, e.codigo))
                    continue
                if inicio is None:
                    salida.append(
                        resultado(
                            camara, Estado.DEGRADADO, "sin_grabaciones", retencion_real_dias=0
                        )
                    )
                    continue
                dias = round((ahora - inicio).total_seconds() / 86400, 1)
                if contratada is not None and dias < float(contratada):
                    salida.append(
                        resultado(
                            camara,
                            Estado.DEGRADADO,
                            "retencion_insuficiente",
                            retencion_real_dias=dias,
                        )
                    )
                else:
                    salida.append(resultado(camara, Estado.OK, retencion_real_dias=dias))
        except httpx.TransportError:
            return [
                resultado(c, Estado.DESCONOCIDO, "sin_respuesta_http")
                for c in objetivo.canales.values()
            ]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()
        return salida

    @staticmethod
    async def _grabacion_mas_antigua(
        cliente: ClienteSoloLectura, canal: int, ahora: datetime, zona: str
    ) -> datetime | None:
        ruta = "/cgi-bin/mediaFileFind.cgi"
        r = await cliente.solicitar("GET", ruta, params={"action": "factory.create"})
        if r.status_code == 401:
            raise ErrorDahua(Estado.DESCONOCIDO, "credencial_rechazada")
        objeto = clave_valor(r.text).get("result")
        if r.status_code != 200 or not objeto or not objeto.isdigit():
            raise ErrorDahua(Estado.DESCONOCIDO, "busqueda_no_soportada")
        try:
            hasta = ahora.astimezone(ZoneInfo(zona)).strftime(FORMATO_FECHA)
            r = await cliente.solicitar(
                "GET",
                ruta,
                params={
                    "action": "findFile",
                    "object": objeto,
                    "condition.Channel": str(canal),
                    "condition.StartTime": "2000-01-01 00:00:00",
                    "condition.EndTime": hasta,
                    "condition.Types[0]": "dav",
                },
            )
            if r.status_code != 200:
                raise ErrorDahua(Estado.DESCONOCIDO, "busqueda_no_soportada")
            r = await cliente.solicitar(
                "GET", ruta, params={"action": "findNextFile", "object": objeto, "count": "1"}
            )
            datos = clave_valor(r.text) if r.status_code == 200 else {}
            if datos.get("found", "0") in ("0", ""):
                return None
            texto = datos.get("items[0].StartTime")
            return hora_local_a_utc(texto, zona) if texto else None
        finally:
            for accion in ("close", "destroy"):
                await cliente.solicitar("GET", ruta, params={"action": accion, "object": objeto})

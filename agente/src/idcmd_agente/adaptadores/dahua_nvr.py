"""NVR Dahua: salud del grabador (`dahua_nvr`) y retención real por cámara (`dahua_retencion`).

`dahua_nvr` (cada 60 s, cuenta para la detección):
    * identidad del NVR: modelo, firmware, serie, reloj;
    * discos: con error, sin disco, porcentaje de uso;
    * por canal: si el NVR ve la cámara conectada (resultado a nombre de la cámara).

`dahua_retencion` (cada 15 min, `deteccion: false`):
    * por canal: fecha de la grabación más antigua -> días reales de retención;
    * por canal: si hay grabación en las últimas `ventana_grabacion_reciente_h` horas. La más
      antigua sola no basta: en la oficina el canal 3 tenía archivos de junio de una cámara
      anterior y "120 días" aunque llevaba semanas sin grabar (2026-10-27);
    * retención proyectada (días reales / fracción de disco usada) mientras el disco no se
      llena, y comparación con `retencion_contratada_dias` (ver juzgar_retencion).

Ambos son "de sistema": el objetivo trae `canales: {canal: dispositivo_id_de_la_camara}`.
Solo leen: nada de video en vivo, reproducción, descarga ni configuración. En particular no
usan `configManager.cgi` (RemoteDevice devuelve las contraseñas de las cámaras).

Parámetros:
    canal_base              numeración de canales del NVR en la API (1 por defecto)
    retencion_contratada_dias
    almacenamiento_esperado_gb  capacidad instalada; si baja más de 5 % -> capacidad_reducida
    umbral_uso_disco_pct    solo para NVR SIN sobrescritura: alerta de disco casi lleno
    ventana_grabacion_reciente_h  2 por defecto; con grabación solo por movimiento, subirla
                            (un pasillo sin movimiento de noche no es una falla)
    esquema, verificar_tls, umbral_desfase_s, zona_horaria_equipo   como en dahua_acceso

PENDIENTE DE VALIDAR con el NVR de la oficina: formatos de getDeviceAllInfo,
getCameraState y mediaFileFind varían por firmware (tests/fixtures/dahua_nvr/).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, ResultadoChequeo, ahora_utc
from .base import registrar
from .dahua_comun import (
    CODIGOS_ACCESO,
    FORMATO_FECHA,
    OPS_IDENTIDAD,
    PATRON_FECHA,
    ErrorDahua,
    clave_valor,
    crear_cliente,
    hora_local_a_utc,
    leer_identidad,
    verificar_acceso,
)
from .http_seguro import ClienteSoloLectura, Operacion

OPS_NVR = [
    *OPS_IDENTIDAD,
    Operacion("GET", r"/cgi-bin/storageDevice\.cgi", {"action": r"getDeviceAllInfo"}),
    Operacion("POST", r"/cgi-bin/api/LogicDeviceManager/getCameraState"),
]

_OBJETO = r"\d{1,12}"
OPS_RETENCION = [
    Operacion("GET", r"/cgi-bin/storageDevice\.cgi", {"action": r"getDeviceAllInfo"}),
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


ORDEN_HALLAZGOS_NVR = [
    "disco_con_error",
    "sin_disco",
    "capacidad_reducida",
    "disco_casi_lleno",
    "reloj_desfasado",
]
LLENO_PCT = 97  # desde aquí el NVR está sobrescribiendo: la retención real es su techo


def evaluar_almacenamiento(discos: list[dict], p: dict, metricas: dict) -> list[str]:
    """Completa las métricas de almacenamiento y devuelve los hallazgos.

    Un NVR al 100 % NO es una falla: lo normal es que sobrescriba lo más antiguo. Las
    alertas son: disco con error, sin disco, capacidad menor a la instalada (se retiró o
    se murió un disco) y, solo si se configura `umbral_uso_disco_pct` (NVR sin
    sobrescritura), disco casi lleno.
    """
    hallazgos: list[str] = []
    total = sum(d["total"] for d in discos)
    usado = sum(d["usado"] for d in discos)
    metricas["discos_total"] = len(discos)
    metricas["discos_con_error"] = sum(d["error"] for d in discos)
    if total:
        metricas["almacenamiento_uso_pct"] = round(100 * usado / total, 1)
        metricas["almacenamiento_total_gb"] = round(total / 1e9)
        metricas["almacenamiento_libre_gb"] = round((total - usado) / 1e9)
    if not discos:
        metricas["hdd_estado"] = "sin_disco"
        hallazgos.append("sin_disco")
    elif metricas["discos_con_error"]:
        metricas["hdd_estado"] = "error"
        hallazgos.append("disco_con_error")
    else:
        metricas["hdd_estado"] = "normal"
    esperado = p.get("almacenamiento_esperado_gb")
    if esperado is not None and total / 1e9 < 0.95 * float(esperado):
        hallazgos.append("capacidad_reducida")
    umbral = p.get("umbral_uso_disco_pct")
    if umbral is not None and total and 100 * usado / total >= float(umbral):
        hallazgos.append("disco_casi_lleno")
    return hallazgos


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
            return [resultado(nvr, "dahua_nvr", e.estado, e.codigo, **e.metricas)]
        except httpx.TransportError:
            return [resultado(nvr, "dahua_nvr", Estado.CAIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        if r_discos.status_code == 200:
            hallazgos += evaluar_almacenamiento(
                interpretar_discos(r_discos.text), objetivo.parametros, metricas
            )
        hallazgos.sort(key=ORDEN_HALLAZGOS_NVR.index)

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
            elif estado.lower() == "empty":
                # "Empty": el canal ya no tiene cámara asignada en el NVR (alguien la quitó o
                # la movió de canal; visto en la oficina, 2026-10-27). No graba igual que una
                # caída, pero la causa es una configuración, no un cable ni un PoE.
                salida.append(
                    resultado(camara, "dahua_nvr_canal", Estado.CAIDO, "canal_sin_camara")
                )
            else:
                salida.append(
                    resultado(camara, "dahua_nvr_canal", Estado.CAIDO, "camara_desconectada_nvr")
                )
        return salida


def juzgar_retencion(
    dias: float, uso_pct: float | None, contratada: object
) -> tuple[Estado, str | None, dict]:
    """Retención real y proyectada frente a la contratada.

    Mientras el disco no está lleno, la retención real es solo la antigüedad de la
    grabación (un NVR nuevo tiene pocos días y eso no es una falla): se juzga con la
    PROYECTADA = días reales / fracción de disco usada. Con el disco lleno (sobrescribiendo)
    la real es el techo y se juzga con ella. Si no se conoce el uso, se juzga con la real.
    """
    metricas: dict = {"retencion_real_dias": dias}
    lleno = uso_pct is None or uso_pct >= LLENO_PCT
    if not lleno and uso_pct > 0:
        metricas["retencion_proyectada_dias"] = round(dias / (uso_pct / 100), 1)
    if contratada is None:
        return Estado.OK, None, metricas
    if lleno and dias < float(contratada):
        return Estado.DEGRADADO, "retencion_insuficiente", metricas
    proyectada = metricas.get("retencion_proyectada_dias")
    if proyectada is not None and proyectada < float(contratada):
        return Estado.DEGRADADO, "retencion_proyectada_insuficiente", metricas
    return Estado.OK, None, metricas


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
        base = int(p.get("canal_base", 1))
        ventana_reciente = timedelta(hours=float(p.get("ventana_grabacion_reciente_h", 2)))
        salida = []
        try:
            uso = await self._uso_disco(cliente)
            for canal, camara in sorted(objetivo.canales.items()):
                try:
                    inicio = await self._primera_grabacion(
                        cliente,
                        canal,
                        base,
                        datetime(2000, 1, 1, tzinfo=ZoneInfo(zona)),
                        ahora,
                        zona,
                    )
                    reciente = inicio and await self._primera_grabacion(
                        cliente, canal, base, ahora - ventana_reciente, ahora, zona
                    )
                except ErrorDahua as e:
                    if e.codigo in CODIGOS_ACCESO:
                        raise
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
                estado, codigo, metricas = juzgar_retencion(dias, uso, contratada)
                if reciente is None:
                    # Hay video viejo pero no nuevo: la cámara dejó de grabar. Pesa más que
                    # cualquier juicio de retención.
                    estado, codigo = Estado.DEGRADADO, "sin_grabacion_reciente"
                salida.append(resultado(camara, estado, codigo, **metricas))
        except ErrorDahua as e:
            # Sin acceso al equipo: se informa para todas las cámaras y no se insiste.
            return [
                resultado(c, e.estado, e.codigo, **e.metricas) for c in objetivo.canales.values()
            ]
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
    async def _uso_disco(cliente: ClienteSoloLectura) -> float | None:
        r = await cliente.solicitar(
            "GET", "/cgi-bin/storageDevice.cgi", params={"action": "getDeviceAllInfo"}
        )
        verificar_acceso(r)
        if r.status_code != 200:
            return None
        discos = interpretar_discos(r.text)
        total = sum(d["total"] for d in discos)
        return 100 * sum(d["usado"] for d in discos) / total if total else None

    @staticmethod
    async def _primera_grabacion(
        cliente: ClienteSoloLectura,
        canal: int,
        base: int,
        desde: datetime,
        hasta: datetime,
        zona: str,
    ) -> datetime | None:
        """Inicio del primer archivo del canal entre `desde` y `hasta`, o None si no hay.

        La búsqueda se pide con el canal numerado desde 1 y el NVR responde numerado desde 0
        (`items[0].Channel=2` para el canal 3, NVR5432 fw 5.001). Si el canal de la respuesta
        no es el pedido, el firmware ignoró el filtro: mejor no informar que informar la
        retención de otra cámara.
        """
        ruta = "/cgi-bin/mediaFileFind.cgi"
        r = await cliente.solicitar("GET", ruta, params={"action": "factory.create"})
        verificar_acceso(r)
        objeto = clave_valor(r.text).get("result")
        if r.status_code != 200 or not objeto or not objeto.isdigit():
            raise ErrorDahua(Estado.DESCONOCIDO, "busqueda_no_soportada")
        local = ZoneInfo(zona)
        try:
            r = await cliente.solicitar(
                "GET",
                ruta,
                params={
                    "action": "findFile",
                    "object": objeto,
                    "condition.Channel": str(canal),
                    "condition.StartTime": desde.astimezone(local).strftime(FORMATO_FECHA),
                    "condition.EndTime": hasta.astimezone(local).strftime(FORMATO_FECHA),
                    "condition.Types[0]": "dav",
                },
            )
            if r.status_code != 200:
                # Sin archivos en el rango, algunos firmwares responden error en findFile.
                if desde.year > 2000:
                    return None
                raise ErrorDahua(Estado.DESCONOCIDO, "busqueda_no_soportada")
            r = await cliente.solicitar(
                "GET", ruta, params={"action": "findNextFile", "object": objeto, "count": "1"}
            )
            datos = clave_valor(r.text) if r.status_code == 200 else {}
            if datos.get("found", "0") in ("0", ""):
                return None
            canal_respuesta = datos.get("items[0].Channel", "")
            if canal_respuesta.isdigit() and int(canal_respuesta) != canal - base:
                raise ErrorDahua(Estado.DESCONOCIDO, "busqueda_canal_inconsistente")
            texto = datos.get("items[0].StartTime")
            return hora_local_a_utc(texto, zona) if texto else None
        finally:
            for accion in ("close", "destroy"):
                await cliente.solicitar("GET", ruta, params={"action": accion, "object": objeto})

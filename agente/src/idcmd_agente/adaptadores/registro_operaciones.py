"""Registro de operaciones administrativas de los equipos (ADR 0009).

Quién entró a un equipo (cuenta e IP de origen) y qué hizo: iniciar sesión, cambiar la
configuración, buscar, reproducir o exportar video, borrar registros, reiniciar, actualizar
el firmware. Es la auditoría que detecta a quien entra con `admin` a borrar video o apagar
cámaras.

`dahua_registro` (log.cgi) y `hikvision_registro` (ContentMgmt/logSearch) son objetivos
aparte, cada 5 min y con `deteccion: false`. Reglas que se imponen aquí, en el sitio:

* Solo se envían entradas de OPERACIÓN. El registro de un panel de acceso puede incluir
  eventos de marcación con número de tarjeta o de empleado: se descartan en el agente y
  nunca salen de la red del cliente.
* Las consultas del propio agente no ensucian la auditoría: se omiten los inicios de sesión
  y consultas de la cuenta del agente, PERO no sus operaciones críticas (cambiar
  configuración, borrar registros, exportar, reiniciar): si alguien usa la credencial del
  agente para eso, se reporta.
* Los EVENTOS del equipo (analítica IVS, detección de audio o de movimiento, alarmas) no son
  operaciones aunque el registro los anote a nombre de `admin`: se descartan. En el NVR de la
  oficina eran ~200 por hora y tapaban los inicios de sesión.
* Los textos vienen en el idioma del equipo ("Detección de audio"): se clasifican en
  español e inglés, sin tildes.
* No se pierden entradas: si una lectura llega al tope (`MAX_ENTRADAS`), la siguiente sigue
  desde la última entrada leída en vez de saltar hasta ahora.
* La hora se interpreta como hora de pared en `zona_horaria_equipo`: hay equipos con la
  zona mal configurada (el DS-K2602T de la oficina dice UTC+0 estando en Bogotá).

PENDIENTE DE VALIDAR con los equipos de la oficina: los formatos de ambos registros varían
por firmware y la documentación pública es incompleta (`idcmd sondear --capturar`).
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..boveda import Credencial
from ..nucleo.modelos import Estado, Objetivo, OperacionEquipo, ResultadoChequeo, ahora_utc
from .base import registrar
from .comun import texto_seguro
from .dahua_comun import PATRON_FECHA, ErrorDahua, clave_valor, crear_cliente, verificar_acceso
from .http_seguro import ClienteSoloLectura, Operacion

_NS = uuid.UUID("5e8b7a2c-3d14-4c9b-8f21-6a0e9d4b7c31")
VENTANA_INICIAL = timedelta(hours=24)
SOLAPE = timedelta(minutes=10)  # se vuelve a leer un poco hacia atrás; los ids deduplican
MAX_ENTRADAS = 2000

# Operaciones de la cuenta del agente que SÍ se reportan: si alguien usa su credencial para
# esto (o la adivina), debe saberse. Un inicio de sesión fallido con la cuenta del agente
# también es la antesala de un bloqueo.
CRITICAS = frozenset(
    {
        "inicio_sesion_fallido",
        "cambio_configuracion",
        "borrado_registros",
        "exportacion_video",
        "reinicio",
        "actualizacion_firmware",
    }
)

# (palabras clave, tipo), en orden: la primera que coincide gana. Inglés y español sin
# tildes (el texto se normaliza antes de comparar).
_REGLAS: list[tuple[tuple[str, ...], str]] = [
    # Gestión de cuentas del equipo ("Añadir Usuario" en el NVR de la oficina): es un cambio
    # de configuración de seguridad. Va primero para que "Eliminar usuario" no se tome por
    # borrado de registros.
    (
        (
            "anadir usuario", "agregar usuario", "eliminar usuario", "borrar usuario",
            "modificar usuario", "add user", "adduser", "delete user", "deleteuser",
            "modify user", "modifyuser", "contrasena", "password",
        ),
        "cambio_configuracion",
    ),
    (
        ("clear", "dellog", "delete log", "deletelog", "borrar", "limpiar", "eliminar"),
        "borrado_registros",
    ),
    (("upgrade", "firmware", "actualizacion", "actualizar"), "actualizacion_firmware"),
    (("reboot", "restart", "shutdown", "reinici", "apagar", "apagado"), "reinicio"),
    (
        ("export", "backup", "download", "respaldo", "copia de seguridad", "descarga"),
        "exportacion_video",
    ),
    (("playback", "reproduc"), "reproduccion_video"),
    # El NVR de la oficina escribe "Cierre ssión usua" (truncado y con errata) y
    # "Usuario conectado.". "desconectado" va antes porque contiene "conectado".
    (
        ("logout", "logoff", "cerrar sesion", "cierre s", "salir", "desconect", "disconnect"),
        "cierre_sesion",
    ),
    (("login", "logon", "sesion", "conectado", "connected"), "inicio_sesion"),
    # Consultas de parámetros (lo que hace el propio agente): no son cambios.
    (("getparm", "getparam", "getconfig", "get config", "getcfg", "read"), "otra"),
    (("search", "query", "find", "busqueda", "buscar", "consulta"), "busqueda_video"),
    (
        (
            "config", "cfg", "setparm", "setparam", "parm", "param", "modify", "save",
            "guardar", "modific", "cambi",
        ),
        "cambio_configuracion",
    ),
]  # fmt: skip
_INICIO_SESION = ("login", "logon", "inicio de sesion", "iniciar sesion")
_FALLO = ("fail", "error", "illegal", "invalid", "wrong", "locked", "fall", "incorrect", "bloque")

# Eventos del equipo, no operaciones de una persona: se descartan si no son una operación.
_EVENTOS = (
    "ivs", "detecc", "detect", "alarm", "motion", "movimiento", "videoloss", "video loss",
    "perdida de video", "tamper", "manipulacion", "audio", "smd", "intrusion", "tripwire",
    "cruce de linea", "rostro", "face", "persona", "people", "vehicul", "vehicle", "heat",
    "event",
)  # fmt: skip


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.lower().replace("_", "")


def clasificar(texto: str) -> str:
    t = _normalizar(texto)
    if any(i in t for i in _INICIO_SESION) and any(f in t for f in _FALLO):
        return "inicio_sesion_fallido"
    for palabras, tipo in _REGLAS:
        if any(p.replace("_", "") in t for p in palabras):
            return tipo
    return "otra"


def es_evento(texto: str) -> bool:
    """Analítica, alarmas y detecciones: el equipo las anota, ninguna persona las hizo."""
    t = _normalizar(texto)
    return clasificar(texto) == "otra" and any(e in t for e in _EVENTOS)


def _ip(valor: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((valor or "").strip()))
    except ValueError:
        return None


def construir_operacion(
    dispositivo: uuid.UUID,
    ocurrido: datetime,
    texto_tipo: str,
    cuenta: str | None,
    ip: str | None,
    cuenta_agente: str | None,
    ip_agente: str | None = None,
) -> OperacionEquipo | None:
    if es_evento(texto_tipo):
        return None
    tipo = clasificar(texto_tipo)
    if tipo not in CRITICAS:
        # Las propias consultas del agente: por su cuenta propia o, si la cuenta es
        # compartida (ver `_identidad_agente`), por la IP desde la que consulta.
        if cuenta and cuenta_agente and cuenta == cuenta_agente:
            return None
        if ip_agente and _ip(ip) == ip_agente:
            return None
    cuenta_txt, codigo = texto_seguro(cuenta), texto_seguro(texto_tipo)
    ip_txt = _ip(ip)
    clave = f"{dispositivo}|{ocurrido.isoformat()}|{codigo}|{cuenta_txt}|{ip_txt}"
    return OperacionEquipo(
        id=uuid.uuid5(_NS, clave),
        dispositivo_id=dispositivo,
        ocurrido_at=ocurrido,
        tipo=tipo,
        cuenta=cuenta_txt,
        ip_origen=ip_txt,
        codigo_fabricante=codigo,
    )


class _BaseRegistro:
    chequeo: ClassVar[str]
    capacidades: ClassVar[frozenset[str]] = frozenset({"auditoria"})

    def __init__(
        self,
        cliente_http: httpx.AsyncClient | None = None,
        reloj: Callable[[], datetime] = ahora_utc,
        capturar_en: Path | None = None,
    ):
        self._cliente_http = cliente_http
        self._reloj = reloj
        self._capturar_en = capturar_en
        # dispositivo -> (desde dónde seguir, si se aplica el solape)
        self._cursor: dict[uuid.UUID, tuple[datetime, bool]] = {}

    def _ventana(self, objetivo: Objetivo) -> tuple[datetime, datetime, ZoneInfo]:
        zona = ZoneInfo(str(objetivo.parametros.get("zona_horaria_equipo", "America/Bogota")))
        fin = self._reloj()
        cursor = self._cursor.get(objetivo.dispositivo_id)
        if cursor is None:
            # Primera lectura. En laboratorio (`sondear`, una sola pasada) conviene acortarla:
            # el NVR de la oficina anota ~4800 entradas por día.
            horas = objetivo.parametros.get("ventana_inicial_h")
            ventana = timedelta(hours=float(horas)) if horas else VENTANA_INICIAL
            return fin - ventana, fin, zona
        desde, con_solape = cursor
        return (desde - SOLAPE if con_solape else desde), fin, zona

    def _avanzar(
        self, objetivo: Objetivo, inicio: datetime, fin: datetime, horas: list[datetime], tope: bool
    ) -> None:
        """Con el tope alcanzado quedan entradas sin leer: se sigue desde la última leída (los
        ids deduplican la del borde). Si todas caen en el mismo segundo no hay cómo avanzar
        por hora y se salta hasta `fin` para no quedarse atascado."""
        ultima = max(horas, default=None)
        if tope and ultima is not None and ultima > inicio:
            self._cursor[objetivo.dispositivo_id] = (ultima, False)
        else:
            self._cursor[objetivo.dispositivo_id] = (fin, True)

    @staticmethod
    def _identidad_agente(
        objetivo: Objetivo, credencial: Credencial
    ) -> tuple[str | None, str | None]:
        """(cuenta, ip) con que se reconocen las consultas del propio agente.

        Hay equipos que solo admiten la cuenta `admin` (el DS-K2602T de la oficina, sin
        forma de agregar usuarios). Con `cuenta_compartida: true` la cuenta deja de
        servir para separar al agente de las personas: omitir "admin" ocultaría los accesos
        humanos, que son justo lo que se audita. Entonces se separa por `ip_agente` (la IP
        del mini PC vista por el equipo); sin ella se reporta todo, ruido antes que ceguera.
        """
        p = objetivo.parametros
        if p.get("cuenta_compartida"):
            return None, _ip(str(p.get("ip_agente", "")))
        return credencial.usuario, None

    def _resultado(self, objetivo, estado, codigo=None, **metricas) -> ResultadoChequeo:
        return ResultadoChequeo(
            dispositivo_id=objetivo.dispositivo_id,
            chequeo=self.chequeo,
            estado=estado,
            observado_at=self._reloj(),
            codigo=codigo,
            metricas=metricas,
        )


# --- Dahua -------------------------------------------------------------------------------

OPS_DAHUA = [
    Operacion(
        "GET",
        r"/cgi-bin/log\.cgi",
        {
            "action": r"startFind",
            "condition.StartTime": PATRON_FECHA,
            "condition.EndTime": PATRON_FECHA,
        },
    ),
    Operacion(
        "GET",
        r"/cgi-bin/log\.cgi",
        {"action": r"doFind", "token": r"\d{1,12}", "count": r"\d{1,3}"},
    ),
    Operacion("GET", r"/cgi-bin/log\.cgi", {"action": r"stopFind", "token": r"\d{1,12}"}),
]
_ITEM = re.compile(r"^items\[(\d+)\]\.(.+)$")
_FORMATO_DAHUA = "%Y-%m-%d %H:%M:%S"


def interpretar_registro_dahua(texto: str) -> list[dict[str, str]]:
    items: dict[int, dict[str, str]] = {}
    for clave, valor in clave_valor(texto).items():
        if m := _ITEM.match(clave):
            items.setdefault(int(m.group(1)), {})[m.group(2)] = valor
    return [items[i] for i in sorted(items)]


def _campo(item: dict[str, str], *pistas: str) -> str | None:
    for clave, valor in item.items():
        if any(p in clave.lower() for p in pistas) and valor:
            return valor
    return None


_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def ip_de_entrada(item: dict[str, str]) -> str | None:
    """El DHI-NVR5432-EI (fw 5.001) no tiene campo de IP: la escribe en el texto libre,
    `Detail=Dirección IP de inicio de sesión:192.168.1.9`. Se toma solo si el texto habla
    de IP, para no confundir otras cifras con una dirección."""
    if directa := _campo(item, "ip", "address"):
        return directa
    for clave, valor in item.items():
        if "detail" in clave.lower() and "ip" in _normalizar(valor):
            if m := _IPV4.search(valor):
                return m.group(1)
    return None


EMPAREJAR = timedelta(seconds=15)


def unir_pares(operaciones: list[OperacionEquipo]) -> list[OperacionEquipo]:
    """Dahua anota cada inicio de sesión en DOS entradas seguidas (una con el grupo, otra
    con la IP, ~6 s después) y cada fallo también en dos ("Illegal Login" y "Fallo al
    iniciar sesión."). Se unen de a pares, no en bloque: diez intentos fallidos seguidos
    siguen siendo diez, que es lo que delata un ataque de adivinación."""
    ordenadas = sorted(operaciones, key=lambda o: o.ocurrido_at)
    salida: list[OperacionEquipo] = []
    i = 0
    while i < len(ordenadas):
        a = ordenadas[i]
        b = ordenadas[i + 1] if i + 1 < len(ordenadas) else None
        if (
            b is not None
            and (a.tipo, a.cuenta) == (b.tipo, b.cuenta)
            and b.ocurrido_at - a.ocurrido_at <= EMPAREJAR
            and (a.ip_origen is None or b.ip_origen is None)
        ):
            salida.append(b if b.ip_origen else a)
            i += 2
            continue
        salida.append(a)
        i += 1
    return salida


@registrar("dahua_registro")
class AdaptadorDahuaRegistro(_BaseRegistro):
    chequeo = "dahua_registro"

    async def chequear(self, objetivo: Objetivo, credencial: Credencial | None):
        if credencial is None:
            return [self._resultado(objetivo, Estado.DESCONOCIDO, "credencial_requerida")]
        inicio, fin, zona = self._ventana(objetivo)
        cliente = crear_cliente(
            objetivo, credencial, OPS_DAHUA, self._cliente_http, self._capturar_en
        )
        ruta = "/cgi-bin/log.cgi"
        try:
            r = await cliente.solicitar(
                "GET",
                ruta,
                params={
                    "action": "startFind",
                    "condition.StartTime": inicio.astimezone(zona).strftime(_FORMATO_DAHUA),
                    "condition.EndTime": fin.astimezone(zona).strftime(_FORMATO_DAHUA),
                },
            )
            verificar_acceso(r)
            token = clave_valor(r.text).get("token", "") if r.status_code == 200 else ""
            if not token.isdigit():
                return [self._resultado(objetivo, Estado.DESCONOCIDO, "registro_no_soportado")]
            items: list[dict[str, str]] = []
            try:
                while len(items) < MAX_ENTRADAS:
                    r = await cliente.solicitar(
                        "GET", ruta, params={"action": "doFind", "token": token, "count": "100"}
                    )
                    lote = interpretar_registro_dahua(r.text) if r.status_code == 200 else []
                    items += lote
                    if len(lote) < 100:
                        break
            finally:
                await cliente.solicitar("GET", ruta, params={"action": "stopFind", "token": token})
        except ErrorDahua as e:
            return [self._resultado(objetivo, e.estado, e.codigo, **e.metricas)]
        except httpx.TransportError:
            return [self._resultado(objetivo, Estado.DESCONOCIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        operaciones, horas = [], []
        for item in items:
            try:
                ocurrido = datetime.strptime(item.get("Time", ""), _FORMATO_DAHUA)
            except ValueError:
                continue
            ocurrido = ocurrido.replace(tzinfo=zona)
            horas.append(ocurrido)
            texto = " ".join(
                v for k, v in item.items() if "type" in k.lower() or "action" in k.lower()
            )
            op = construir_operacion(
                objetivo.dispositivo_id,
                ocurrido,
                texto or "desconocido",
                item.get("User") or _campo(item, "user"),
                ip_de_entrada(item),
                *self._identidad_agente(objetivo, credencial),
            )
            if op is not None:
                operaciones.append(op)
        self._avanzar(objetivo, inicio, fin, horas, len(items) >= MAX_ENTRADAS)
        return [self._resultado(objetivo, Estado.OK), *unir_pares(operaciones)]


# --- Hikvision ---------------------------------------------------------------------------

OPS_HIKVISION = [Operacion("POST", r"/ISAPI/ContentMgmt/logSearch")]
_BUSQUEDA = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
<searchID>{id}</searchID>
<metaId>log.std-cgi.com</metaId>
<timeSpanList><timeSpan><startTime>{inicio}</startTime><endTime>{fin}</endTime></timeSpan>
</timeSpanList>
<maxResults>50</maxResults>
<searchResultPostion>{posicion}</searchResultPostion>
</CMSearchDescription>"""


def interpretar_registro_hikvision(texto: bytes) -> tuple[list[dict[str, str]], bool]:
    """(entradas, hay_mas). Cada entrada: hojas del logDescriptor sin espacio de nombres."""
    raiz = ET.fromstring(texto)

    def nombre(el):
        return el.tag.rsplit("}", 1)[-1]

    entradas = []
    for item in raiz.iter():
        if nombre(item) == "searchMatchItem":
            entradas.append({nombre(e): (e.text or "").strip() for e in item.iter()})
    estado = next((e.text or "" for e in raiz.iter() if nombre(e) == "responseStatusStrg"), "")
    return entradas, estado.upper() == "MORE"


@registrar("hikvision_registro")
class AdaptadorHikvisionRegistro(_BaseRegistro):
    chequeo = "hikvision_registro"

    async def chequear(self, objetivo: Objetivo, credencial: Credencial | None):
        if credencial is None:
            return [self._resultado(objetivo, Estado.DESCONOCIDO, "credencial_requerida")]
        p = objetivo.parametros
        inicio, fin, zona = self._ventana(objetivo)
        cliente = ClienteSoloLectura(
            f"{p.get('esquema', 'http')}://{objetivo.direccion}",
            OPS_HIKVISION,
            cliente=self._cliente_http,
            auth=httpx.DigestAuth(credencial.usuario, credencial.clave.get_secret_value()),
            verificar_tls=bool(p.get("verificar_tls", False)),
            capturar_en=self._capturar_en,
        )
        formato = "%Y-%m-%dT%H:%M:%SZ"
        entradas: list[dict[str, str]] = []
        try:
            while len(entradas) < MAX_ENTRADAS:
                cuerpo = _BUSQUEDA.format(
                    id=uuid.uuid4(),
                    posicion=len(entradas),
                    inicio=inicio.astimezone(zona).strftime(formato),
                    fin=fin.astimezone(zona).strftime(formato),
                )
                r = await cliente.solicitar(
                    "POST",
                    "/ISAPI/ContentMgmt/logSearch",
                    content=cuerpo.encode(),
                    headers={"Content-Type": "application/xml"},
                )
                if r.status_code != 200:
                    if not entradas:
                        return [
                            self._resultado(objetivo, Estado.DESCONOCIDO, "registro_no_soportado")
                        ]
                    break
                try:
                    lote, hay_mas = interpretar_registro_hikvision(r.content)
                except ET.ParseError:
                    return [
                        self._resultado(objetivo, Estado.DESCONOCIDO, "respuesta_no_reconocida")
                    ]
                entradas += lote
                if not hay_mas or not lote:
                    break
        except httpx.TransportError:
            return [self._resultado(objetivo, Estado.DESCONOCIDO, "sin_respuesta_http")]
        finally:
            if self._cliente_http is None:
                await cliente.cerrar()

        operaciones, horas = [], []
        for e in entradas:
            meta = e.get("metaId", "")
            try:
                ocurrido = datetime.fromisoformat(e.get("StartDateTime", "").replace("Z", ""))
            except ValueError:
                continue
            ocurrido = ocurrido.replace(tzinfo=None).replace(tzinfo=zona)
            horas.append(ocurrido)
            # Solo operaciones. Eventos (p. ej. marcaciones con tarjeta) se descartan aquí.
            if "/operation" not in meta.lower():
                continue
            op = construir_operacion(
                objetivo.dispositivo_id,
                ocurrido,
                meta.rsplit("/", 1)[-1],
                e.get("userName"),
                e.get("ipAddress"),
                *self._identidad_agente(objetivo, credencial),
            )
            if op is not None:
                operaciones.append(op)
        self._avanzar(objetivo, inicio, fin, horas, len(entradas) >= MAX_ENTRADAS)
        return [self._resultado(objetivo, Estado.OK), *operaciones]

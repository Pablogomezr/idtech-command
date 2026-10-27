"""Auditoría de accesos administrativos (ADR 0009, opción A: cuenta e IP de origen)."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from conftest import RelojFalso
from pydantic import SecretStr

from idcmd_agente.adaptadores.http_seguro import ClienteSoloLectura
from idcmd_agente.adaptadores.registro_operaciones import (
    MAX_ENTRADAS,
    OPS_DAHUA,
    OPS_HIKVISION,
    AdaptadorDahuaRegistro,
    AdaptadorHikvisionRegistro,
    clasificar,
    es_evento,
)
from idcmd_agente.boveda import Credencial
from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Objetivo, OperacionEquipo
from idcmd_agente.nucleo.planificador import Planificador

AHORA = datetime(2026, 10, 27, 19, 0, tzinfo=UTC)  # 14:00 en Bogotá
CRED = Credencial(usuario="idcmd_monitor", clave=SecretStr("Clave-Lectura-2026"))
NVR_IP, PANEL_IP = "192.168.1.16", "192.168.1.200"


def _objetivo(adaptador, ip):
    return Objetivo(
        dispositivo_id=uuid.uuid4(),
        adaptador=adaptador,
        direccion=ip,
        intervalo_s=300,
        deteccion=False,
    )


@pytest.mark.parametrize(
    ("texto", "tipo"),
    [
        ("Login", "inicio_sesion"),
        ("remoteLogin", "inicio_sesion"),
        ("LoginFailed", "inicio_sesion_fallido"),
        ("Logout", "cierre_sesion"),
        ("SaveConfig", "cambio_configuracion"),
        ("remoteCfgParm", "cambio_configuracion"),
        ("remoteGetParm", "otra"),  # consultar parámetros (lo que hace el agente) no es cambio
        ("PlayBack", "reproduccion_video"),
        ("remoteDownload", "exportacion_video"),
        ("Backup", "exportacion_video"),
        ("ClearLog", "borrado_registros"),
        ("Reboot", "reinicio"),
        ("Upgrade", "actualizacion_firmware"),
        ("SearchRecord", "busqueda_video"),
        ("algo raro", "otra"),
        # NVR de la oficina en español (2026-10-27).
        ("Inicio de sesión", "inicio_sesion"),
        ("Error de inicio de sesión", "inicio_sesion_fallido"),
        ("Cerrar sesión", "cierre_sesion"),
        ("Guardar configuración", "cambio_configuracion"),
        ("Borrar registro", "borrado_registros"),
        ("Reiniciar", "reinicio"),
        ("Reproducción", "reproduccion_video"),
        ("Respaldo", "exportacion_video"),
        ("Búsqueda de grabaciones", "busqueda_video"),
        # Textos reales del DHI-NVR5432-EI (fw 5.001, 2026-10-27), tal cual los escribe.
        ("Usuario conectado.", "inicio_sesion"),
        ("Cierre ssión usua", "cierre_sesion"),
        ("Fallo al iniciar sesión.", "inicio_sesion_fallido"),
        ("Illegal Login", "inicio_sesion_fallido"),
        ("Añadir Usuario", "cambio_configuracion"),
        ("Eliminar usuario", "cambio_configuracion"),
        ("Usuario desconectado", "cierre_sesion"),
    ],
)
def test_clasificar(texto, tipo):
    assert clasificar(texto) == tipo


# --- Dahua -------------------------------------------------------------------------------

REGISTRO_DAHUA = (
    "found=4\r\n"
    "items[0].Time=2026-10-27 02:14:05\r\nitems[0].Type=Login\r\n"
    "items[0].User=admin\r\nitems[0].Detail.Address=192.168.1.50\r\n"
    "items[1].Time=2026-10-27 02:16:40\r\nitems[1].Type=SaveConfig\r\n"
    "items[1].User=admin\r\nitems[1].Detail.Address=192.168.1.50\r\n"
    "items[2].Time=2026-10-27 13:55:00\r\nitems[2].Type=Login\r\n"
    "items[2].User=idcmd_monitor\r\nitems[2].Detail.Address=192.168.1.10\r\n"
    "items[3].Time=2026-10-27 13:58:00\r\nitems[3].Type=ClearLog\r\n"
    "items[3].User=idcmd_monitor\r\nitems[3].Detail.Address=192.168.1.99\r\n"
)


def _mock_dahua(mock):
    base = f"http://{NVR_IP}/cgi-bin/log.cgi"
    mock.get(base, params={"action": "startFind"}).respond(200, text="token=5\r\n")
    mock.get(base, params={"action": "doFind"}).respond(200, text=REGISTRO_DAHUA)
    return mock.get(base, params={"action": "stopFind"}).respond(200, text="OK\r\n")


async def _correr(clase, objetivo, reloj=None):
    async with httpx.AsyncClient() as cliente:
        adaptador = clase(cliente_http=cliente, reloj=reloj or RelojFalso(AHORA))
        return adaptador, await adaptador.chequear(objetivo, CRED)


async def test_dahua_registra_accesos_y_cambios_de_admin():
    with respx.mock() as mock:
        detener = _mock_dahua(mock)
        _, salida = await _correr(AdaptadorDahuaRegistro, _objetivo("dahua_registro", NVR_IP))
    estado, *ops = salida
    assert estado.estado == Estado.OK and detener.called  # la búsqueda siempre se cierra
    resumen = [(o.tipo, o.cuenta, o.ip_origen) for o in ops]
    assert resumen == [
        ("inicio_sesion", "admin", "192.168.1.50"),
        ("cambio_configuracion", "admin", "192.168.1.50"),
        # El login del agente se omite, pero si su cuenta BORRA registros se reporta.
        ("borrado_registros", "idcmd_monitor", "192.168.1.99"),
    ]
    # 02:14 hora de Bogotá = 07:14 UTC: la madrugada queda bien ubicada para las alertas.
    assert ops[0].ocurrido_at == datetime(2026, 10, 27, 7, 14, 5, tzinfo=UTC)


async def test_dahua_lectura_incremental_sin_duplicados():
    reloj = RelojFalso(AHORA)
    objetivo = _objetivo("dahua_registro", NVR_IP)
    with respx.mock() as mock:
        _mock_dahua(mock)
        async with httpx.AsyncClient() as cliente:
            adaptador = AdaptadorDahuaRegistro(cliente_http=cliente, reloj=reloj)
            primera = await adaptador.chequear(objetivo, CRED)
            reloj.avanzar(300)
            segunda = await adaptador.chequear(objetivo, CRED)
        inicio_2 = next(
            c.request.url for c in reversed(mock.calls) if "startFind" in str(c.request.url)
        )
    # Mismas entradas -> mismos ids: el buzón y la central las deduplican.
    assert [o.id for o in primera[1:]] == [o.id for o in segunda[1:]]
    # La segunda lectura empieza donde terminó la primera, menos el solape de 10 min.
    assert "condition.StartTime=2026-10-27%2013:50:00" in str(inicio_2)


@pytest.mark.parametrize(
    ("texto", "evento"),
    [
        ("IVS", True),
        ("GlobalSence IVS", True),
        ("Detección de audio", True),
        ("Motion Detect", True),
        ("Video Loss", True),
        ("Inicio de sesión", False),
        ("Guardar configuración de eventos", False),  # cambiar la config de eventos SÍ cuenta
        ("algo raro", False),
    ],
)
def test_eventos_del_equipo_no_son_operaciones(texto, evento):
    assert es_evento(texto) is evento


def _items_dahua(entradas):
    lineas = [f"found={len(entradas)}"]
    for i, (hora, tipo, usuario) in enumerate(entradas):
        lineas += [
            f"items[{i}].Time={hora}",
            f"items[{i}].Type={tipo}",
            f"items[{i}].User={usuario}",
        ]
    return "\r\n".join(lineas) + "\r\n"


async def test_dahua_descarta_eventos_ivs_y_audio():
    """El registro del NVR de la oficina: cientos de eventos de analítica a nombre de admin
    y, entre ellos, el inicio de sesión que sí importa."""
    texto = _items_dahua(
        [
            ("2026-10-27 13:00:00", "IVS", "admin"),
            ("2026-10-27 13:00:04", "Detección de audio", "admin"),
            ("2026-10-27 13:01:00", "Inicio de sesión", "admin"),
            ("2026-10-27 13:02:00", "GlobalSence IVS", "admin"),
        ]
    )
    base = f"http://{NVR_IP}/cgi-bin/log.cgi"
    with respx.mock() as mock:
        mock.get(base, params={"action": "startFind"}).respond(200, text="token=5\r\n")
        mock.get(base, params={"action": "doFind"}).respond(200, text=texto)
        mock.get(base, params={"action": "stopFind"}).respond(200, text="OK\r\n")
        _, (_, *ops) = await _correr(AdaptadorDahuaRegistro, _objetivo("dahua_registro", NVR_IP))
    assert [(o.tipo, o.cuenta) for o in ops] == [("inicio_sesion", "admin")]


async def test_dahua_tope_alcanzado_sigue_desde_la_ultima_entrada():
    """Con más entradas que el tope, la lectura siguiente continúa donde quedó: antes se
    saltaba hasta "ahora" y se perdían las más recientes (las del día, con los logins)."""
    paginas = []
    base_hora = datetime(2026, 10, 26, 14, 0)
    for p in range(MAX_ENTRADAS // 100):
        paginas.append(
            _items_dahua(
                [
                    (
                        (base_hora + timedelta(seconds=10 * (p * 100 + i))).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "IVS",
                        "admin",
                    )
                    for i in range(100)
                ]
            )
        )
    ultima = base_hora + timedelta(seconds=10 * (MAX_ENTRADAS - 1))
    reloj = RelojFalso(AHORA)
    objetivo = _objetivo("dahua_registro", NVR_IP)
    base = f"http://{NVR_IP}/cgi-bin/log.cgi"
    with respx.mock() as mock:
        inicio = mock.get(base, params={"action": "startFind"}).respond(200, text="token=5\r\n")
        mock.get(base, params={"action": "doFind"}).mock(
            side_effect=[httpx.Response(200, text=t) for t in paginas]
            + [httpx.Response(200, text="found=0\r\n")]
        )
        mock.get(base, params={"action": "stopFind"}).respond(200, text="OK\r\n")
        async with httpx.AsyncClient() as cliente:
            adaptador = AdaptadorDahuaRegistro(cliente_http=cliente, reloj=reloj)
            await adaptador.chequear(objetivo, CRED)
            reloj.avanzar(300)
            await adaptador.chequear(objetivo, CRED)
    segunda = str(inicio.calls[-1].request.url)
    desde = ultima.strftime("%Y-%m-%d%%20%H:%M:%S")
    assert f"condition.StartTime={desde}" in segunda  # sin solape: sigue exactamente ahí


async def test_dahua_sin_registro():
    with respx.mock() as mock:
        mock.get(f"http://{NVR_IP}/cgi-bin/log.cgi").respond(400)
        _, [r] = await _correr(AdaptadorDahuaRegistro, _objetivo("dahua_registro", NVR_IP))
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "registro_no_soportado")


# --- Hikvision ---------------------------------------------------------------------------

REGISTRO_HIKVISION = b"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.isapi.org/ver20/XMLSchema">
<responseStatusStrg>OK</responseStatusStrg>
<matchList>
<searchMatchItem><logDescriptor><metaId>log.std-cgi.com/Operation/remoteLogin</metaId>
<StartDateTime>2026-10-27T02:14:05Z</StartDateTime><userName>admin</userName>
<ipAddress>192.168.1.50</ipAddress></logDescriptor></searchMatchItem>
<searchMatchItem><logDescriptor><metaId>log.std-cgi.com/Event/cardSwipe</metaId>
<StartDateTime>2026-10-27T08:01:00Z</StartDateTime><userName>Juan Perez</userName>
<cardNo>1234567890</cardNo></logDescriptor></searchMatchItem>
<searchMatchItem><logDescriptor><metaId>log.std-cgi.com/Operation/remoteGetParm</metaId>
<StartDateTime>2026-10-27T13:55:00Z</StartDateTime><userName>idcmd_monitor</userName>
<ipAddress>192.168.1.10</ipAddress></logDescriptor></searchMatchItem>
<searchMatchItem><logDescriptor><metaId>log.std-cgi.com/Operation/remoteCfgParm</metaId>
<StartDateTime>2026-10-27T02:20:00Z</StartDateTime><userName>admin</userName>
<ipAddress>192.168.1.50</ipAddress></logDescriptor></searchMatchItem>
</matchList></CMSearchResult>"""


async def test_hikvision_solo_operaciones_nunca_marcaciones():
    with respx.mock() as mock:
        busqueda = mock.post(f"http://{PANEL_IP}/ISAPI/ContentMgmt/logSearch").respond(
            200, content=REGISTRO_HIKVISION
        )
        _, salida = await _correr(
            AdaptadorHikvisionRegistro, _objetivo("hikvision_registro", PANEL_IP)
        )
    estado, *ops = salida
    assert estado.estado == Estado.OK
    assert [(o.tipo, o.cuenta) for o in ops] == [
        ("inicio_sesion", "admin"),
        ("cambio_configuracion", "admin"),
    ]
    # La marcación de "Juan Perez" con su tarjeta nunca sale del agente.
    texto = " ".join(o.model_dump_json() for o in ops)
    assert "Juan" not in texto and "1234567890" not in texto
    assert b"<metaId>log.std-cgi.com</metaId>" in busqueda.calls[0].request.content


async def test_hikvision_sin_logsearch():
    with respx.mock() as mock:
        mock.post(f"http://{PANEL_IP}/ISAPI/ContentMgmt/logSearch").respond(403)
        _, [r] = await _correr(
            AdaptadorHikvisionRegistro, _objetivo("hikvision_registro", PANEL_IP)
        )
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "registro_no_soportado")


# --- Solo lectura y transporte -----------------------------------------------------------


@pytest.mark.parametrize(
    ("ops", "metodo", "ruta", "params"),
    [
        (OPS_DAHUA, "GET", "/cgi-bin/log.cgi", {"action": "clear"}),  # borraría el registro
        (
            OPS_DAHUA,
            "GET",
            "/cgi-bin/log.cgi",
            {"action": "doFind", "token": "1", "count": "100", "extra": "x"},
        ),
        (OPS_HIKVISION, "PUT", "/ISAPI/ContentMgmt/logSearch", {}),
        (OPS_HIKVISION, "POST", "/ISAPI/ContentMgmt/search", {}),  # búsqueda de video
        (OPS_HIKVISION, "POST", "/ISAPI/AccessControl/AcsEvent", {}),
    ],
)
def test_registro_lista_blanca_bloquea(ops, metodo, ruta, params):
    assert not ClienteSoloLectura("http://x", ops).permitida(metodo, ruta, params)


async def test_planificador_envia_operaciones_al_buzon_y_no_se_purgan(tmp_path):
    objetivo = _objetivo("dahua_registro", NVR_IP)
    buzon = Buzon(tmp_path / "b.sqlite3")
    with respx.mock() as mock:
        _mock_dahua(mock)
        async with httpx.AsyncClient() as cliente:

            class ConCredencial:
                capacidades = frozenset()

                def __init__(self):
                    self._a = AdaptadorDahuaRegistro(cliente_http=cliente, reloj=RelojFalso(AHORA))

                async def chequear(self, o, _):
                    return await self._a.chequear(o, CRED)

            plan = Planificador(
                [objetivo],
                None,
                MaquinaEstados(),
                buzon,
                Agregador(sesion=uuid.uuid4()),
                lambda: None,
                reloj=lambda: AHORA,
                adaptadores={"dahua_registro": ConCredencial()},
            )
            await plan.chequear(objetivo)
    tipos = [i.tipo for i in buzon.tomar_lote(100)]
    assert tipos.count("operacion") == 3
    buzon.purgar(AHORA + timedelta(days=1), max_pendientes=0)
    assert [i.tipo for i in buzon.tomar_lote(100)].count("operacion") == 3


def test_operacion_valida_ip_y_tipo():
    with pytest.raises(ValueError):
        OperacionEquipo(
            id=uuid.uuid4(), dispositivo_id=uuid.uuid4(), ocurrido_at=AHORA, tipo="inventado"
        )
    with pytest.raises(ValueError):
        OperacionEquipo(
            id=uuid.uuid4(),
            dispositivo_id=uuid.uuid4(),
            ocurrido_at=AHORA,
            tipo="inicio_sesion",
            ip_origen="no-es-ip",
        )


async def test_ventana_inicial_configurable_para_laboratorio():
    objetivo = _objetivo("dahua_registro", NVR_IP).model_copy(
        update={"parametros": {"ventana_inicial_h": 4}}
    )
    with respx.mock() as mock:
        _mock_dahua(mock)
        await _correr(AdaptadorDahuaRegistro, objetivo)
        url = str(mock.calls[0].request.url)
    assert "condition.StartTime=2026-10-27%2010:00:00" in url  # 14:00 Bogotá - 4 h


async def test_fallos_de_inicio_de_sesion_de_la_cuenta_del_agente_se_reportan():
    texto = _items_dahua(
        [
            ("2026-10-27 13:00:00", "Usuario conectado.", "idcmd_monitor"),
            ("2026-10-27 13:01:00", "Fallo al iniciar sesión.", "idcmd_monitor"),
        ]
    )
    base = f"http://{NVR_IP}/cgi-bin/log.cgi"
    with respx.mock() as mock:
        mock.get(base, params={"action": "startFind"}).respond(200, text="token=5\r\n")
        mock.get(base, params={"action": "doFind"}).respond(200, text=texto)
        mock.get(base, params={"action": "stopFind"}).respond(200, text="OK\r\n")
        _, (_, *ops) = await _correr(AdaptadorDahuaRegistro, _objetivo("dahua_registro", NVR_IP))
    assert [(o.tipo, o.cuenta) for o in ops] == [("inicio_sesion_fallido", "idcmd_monitor")]

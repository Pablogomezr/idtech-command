"""Adaptadores de control de acceso Hikvision (ISAPI) y Dahua (CGI).

Además del comportamiento, se verifica el principio de solo lectura: ninguna llamada fuera
de la lista blanca sale a la red, en particular marcaciones, apertura de puertas y reinicio.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from conftest import T0, RelojFalso
from pydantic import SecretStr

from idcmd_agente.adaptadores.dahua_acceso import OPERACIONES as OPS_DAHUA
from idcmd_agente.adaptadores.dahua_acceso import AdaptadorDahuaAcceso
from idcmd_agente.adaptadores.hikvision_acceso import OPERACIONES as OPS_HIKVISION
from idcmd_agente.adaptadores.hikvision_acceso import AdaptadorHikvisionAcceso
from idcmd_agente.adaptadores.http_seguro import ClienteSoloLectura, OperacionNoPermitida
from idcmd_agente.boveda import Credencial
from idcmd_agente.nucleo.modelos import Estado, Objetivo

FIXTURES = Path(__file__).parent / "fixtures"
IP = "192.168.1.80"
BASE = f"http://{IP}"
CRED = Credencial(usuario="monitor", clave=SecretStr("Clave-Lectura-2026"))


def _objetivo(adaptador, **parametros):
    return Objetivo(
        dispositivo_id=uuid.uuid4(), adaptador=adaptador, direccion=IP, parametros=parametros
    )


def _leer(ruta):
    return (FIXTURES / ruta).read_bytes()


# --- Hikvision -------------------------------------------------------------------------


def _mock_hikvision(mock, estado_acs=None, hora=None, info_status=200):
    mock.get(f"{BASE}/ISAPI/System/deviceInfo").respond(
        info_status, content=_leer("hikvision_acceso/deviceInfo.xml")
    )
    mock.get(f"{BASE}/ISAPI/System/time").respond(
        200, content=hora or _leer("hikvision_acceso/time.xml")
    )
    acs = json.loads(_leer("hikvision_acceso/AcsWorkStatus.json"))
    acs["AcsWorkStatus"].update(estado_acs or {})
    mock.get(f"{BASE}/ISAPI/AccessControl/AcsWorkStatus", params={"format": "json"}).respond(
        200, json=acs
    )


async def _hikvision(objetivo, credencial=CRED):
    async with httpx.AsyncClient() as cliente:
        adaptador = AdaptadorHikvisionAcceso(cliente_http=cliente, reloj=RelojFalso(T0))
        [r] = await adaptador.chequear(objetivo, credencial)
    return r


async def test_hikvision_sano():
    with respx.mock(assert_all_called=True) as mock:
        _mock_hikvision(mock)
        r = await _hikvision(_objetivo("hikvision_acceso", lectoras_esperadas=2))
    assert (r.estado, r.codigo) == (Estado.OK, None)
    assert r.metricas["modelo"] == "DS-K2604T"
    assert r.metricas["firmware"] == "V2.2.0 build 210106"
    assert r.metricas["desfase_reloj_s"] == 5.0
    assert r.metricas["lectoras_desconectadas"] == 0
    assert r.metricas["tamper_activo"] is False


async def test_hikvision_sin_lectoras_esperadas_no_evalua_puertos_libres():
    with respx.mock() as mock:
        _mock_hikvision(mock)  # reporta [1, 1, 0, 0]: dos puertos sin lectora
        r = await _hikvision(_objetivo("hikvision_acceso"))
    assert r.estado == Estado.OK
    assert "lectoras_desconectadas" not in r.metricas


@pytest.mark.parametrize(
    ("estado_acs", "parametros", "codigo"),
    [
        ({"hostAntiDismantleStatus": "open"}, {}, "tamper_activo"),
        (
            {"cardReaderOnlineStatus": [1, 0, 0, 0]},
            {"lectoras_esperadas": 2},
            "lectora_desconectada",
        ),
        ({"batteryLowVoltage": True}, {}, "bateria_baja"),
        # Tamper y lectora a la vez: se informa el más grave.
        (
            {"hostAntiDismantleStatus": "open", "cardReaderOnlineStatus": [0]},
            {"lectoras_esperadas": 1},
            "tamper_activo",
        ),
    ],
)
async def test_hikvision_degradado(estado_acs, parametros, codigo):
    with respx.mock() as mock:
        _mock_hikvision(mock, estado_acs=estado_acs)
        r = await _hikvision(_objetivo("hikvision_acceso", **parametros))
    assert (r.estado, r.codigo) == (Estado.DEGRADADO, codigo)


async def test_hikvision_reloj_desfasado_y_zona_posix():
    # Hora sin zona, con zona POSIX "CST+5" (UTC-5): 10 minutos adelantado.
    hora = (
        b"<Time><localTime>2026-11-02T03:10:00</localTime><timeZone>CST+5:00:00</timeZone></Time>"
    )
    with respx.mock() as mock:
        _mock_hikvision(mock, hora=hora)
        r = await _hikvision(_objetivo("hikvision_acceso"))
    assert (r.estado, r.codigo) == (Estado.DEGRADADO, "reloj_desfasado")
    assert r.metricas["desfase_reloj_s"] == 600.0


async def test_hikvision_credencial_rechazada():
    with respx.mock() as mock:
        mock.get(f"{BASE}/ISAPI/System/deviceInfo").respond(401)
        r = await _hikvision(_objetivo("hikvision_acceso"))
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "credencial_rechazada")


async def test_hikvision_sin_respuesta_es_caido():
    with respx.mock() as mock:
        mock.get(f"{BASE}/ISAPI/System/deviceInfo").mock(side_effect=httpx.ConnectTimeout("x"))
        r = await _hikvision(_objetivo("hikvision_acceso"))
    assert (r.estado, r.codigo) == (Estado.CAIDO, "sin_respuesta_http")


async def test_hikvision_sin_credencial():
    r = await _hikvision(_objetivo("hikvision_acceso"), credencial=None)
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "credencial_requerida")


async def test_hikvision_terminal_sin_acsworkstatus_sigue_ok():
    with respx.mock() as mock:
        mock.get(f"{BASE}/ISAPI/System/deviceInfo").respond(
            200, content=_leer("hikvision_acceso/deviceInfo.xml")
        )
        mock.get(f"{BASE}/ISAPI/System/time").respond(
            200, content=_leer("hikvision_acceso/time.xml")
        )
        mock.get(f"{BASE}/ISAPI/AccessControl/AcsWorkStatus").respond(404)
        r = await _hikvision(_objetivo("hikvision_acceso"))
    assert r.estado == Estado.OK


async def test_hikvision_solo_llama_lo_permitido():
    with respx.mock() as mock:
        _mock_hikvision(mock)
        await _hikvision(_objetivo("hikvision_acceso"))
        rutas = {c.request.url.path for c in mock.calls}
    assert rutas == {
        "/ISAPI/System/deviceInfo",
        "/ISAPI/System/time",
        "/ISAPI/AccessControl/AcsWorkStatus",
    }


@pytest.mark.parametrize(
    ("metodo", "ruta", "params"),
    [
        ("POST", "/ISAPI/AccessControl/AcsEvent", {"format": "json"}),  # marcaciones
        ("GET", "/ISAPI/AccessControl/AcsEvent", {"format": "json"}),
        ("POST", "/ISAPI/AccessControl/UserInfo/Search", {"format": "json"}),  # personas
        ("GET", "/ISAPI/AccessControl/CardInfo/Count", {"format": "json"}),
        ("PUT", "/ISAPI/AccessControl/RemoteControl/door/1", {}),  # abrir puerta
        ("PUT", "/ISAPI/System/reboot", {}),
        ("PUT", "/ISAPI/System/time", {}),  # cambiar la hora
        ("GET", "/ISAPI/AccessControl/AcsWorkStatus", {"format": "json", "x": "1"}),
        ("GET", "/ISAPI/System/deviceInfo?x=1", {}),
    ],
)
def test_hikvision_lista_blanca_bloquea(metodo, ruta, params):
    assert not ClienteSoloLectura(BASE, OPS_HIKVISION).permitida(metodo, ruta, params)


# --- Dahua -----------------------------------------------------------------------------


def _mock_dahua(mock, hora=None, status_tipo=200):
    for accion in ["getDeviceType", "getSoftwareVersion", "getSerialNo"]:
        mock.get(f"{BASE}/cgi-bin/magicBox.cgi", params={"action": accion}).respond(
            status_tipo if accion == "getDeviceType" else 200,
            content=_leer(f"dahua_acceso/{accion}.txt"),
        )
    mock.get(f"{BASE}/cgi-bin/global.cgi", params={"action": "getCurrentTime"}).respond(
        200, content=hora or _leer("dahua_acceso/getCurrentTime.txt")
    )


async def _dahua(objetivo, credencial=CRED):
    async with httpx.AsyncClient() as cliente:
        adaptador = AdaptadorDahuaAcceso(cliente_http=cliente, reloj=RelojFalso(T0))
        [r] = await adaptador.chequear(objetivo, credencial)
    return r


async def test_dahua_sano():
    with respx.mock(assert_all_called=True) as mock:
        _mock_dahua(mock)
        r = await _dahua(_objetivo("dahua_acceso"))
    assert (r.estado, r.codigo) == (Estado.OK, None)
    assert r.metricas["modelo"] == "ASC1204C-D"
    assert r.metricas["firmware"] == "1.000.0000000.3.R build 2021-03-12"
    assert r.metricas["numero_serie"] == "7A00000000000000"
    assert r.metricas["desfase_reloj_s"] == 5.0  # hora local de Bogotá, 5 s adelantada


async def test_dahua_reloj_desfasado():
    with respx.mock() as mock:
        _mock_dahua(mock, hora=b"result=2026-11-02 02:50:00\r\n")
        r = await _dahua(_objetivo("dahua_acceso"))
    assert (r.estado, r.codigo) == (Estado.DEGRADADO, "reloj_desfasado")
    assert r.metricas["desfase_reloj_s"] == -600.0


async def test_dahua_credencial_rechazada():
    # Tras el 401 no se insiste con las demás consultas.
    with respx.mock(assert_all_called=False) as mock:
        _mock_dahua(mock, status_tipo=401)
        r = await _dahua(_objetivo("dahua_acceso"))
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "credencial_rechazada")


async def test_dahua_sin_respuesta_es_caido():
    with respx.mock() as mock:
        mock.get(f"{BASE}/cgi-bin/magicBox.cgi").mock(side_effect=httpx.ConnectError("x"))
        r = await _dahua(_objetivo("dahua_acceso"))
    assert (r.estado, r.codigo) == (Estado.CAIDO, "sin_respuesta_http")


@pytest.mark.parametrize(
    ("ruta", "params"),
    [
        ("/cgi-bin/magicBox.cgi", {"action": "reboot"}),  # reinicia el equipo
        ("/cgi-bin/magicBox.cgi", {"action": "shutdown"}),
        ("/cgi-bin/accessControl.cgi", {"action": "openDoor", "channel": "1"}),  # abre puerta
        ("/cgi-bin/recordFinder.cgi", {"action": "find", "name": "AccessControlCardRec"}),
        ("/cgi-bin/recordFinder.cgi", {"action": "find", "name": "AccessControlCard"}),
        ("/cgi-bin/configManager.cgi", {"action": "setConfig"}),
        ("/cgi-bin/global.cgi", {"action": "setCurrentTime", "time": "2026-01-01 00:00:00"}),
        # Contrabando de parámetros: uno permitido más otro extra.
        ("/cgi-bin/magicBox.cgi", {"action": "getDeviceType", "extra": "reboot"}),
        # La acción escrita dentro de la ruta en vez de en params.
        ("/cgi-bin/magicBox.cgi?action=reboot", {}),
        ("/cgi-bin/magicBox.cgi", {"action": "getDeviceType|reboot"}),
    ],
)
def test_dahua_lista_blanca_bloquea(ruta, params):
    assert not ClienteSoloLectura(BASE, OPS_DAHUA).permitida("GET", ruta, params)


async def test_lo_prohibido_no_sale_a_la_red():
    with respx.mock(assert_all_mocked=True) as mock:
        cliente = ClienteSoloLectura(BASE, OPS_DAHUA, cliente=httpx.AsyncClient())
        with pytest.raises(OperacionNoPermitida):
            await cliente.solicitar(
                "GET", "/cgi-bin/accessControl.cgi", params={"action": "openDoor"}
            )
        with pytest.raises(OperacionNoPermitida):
            await cliente.solicitar("GET", "/cgi-bin/magicBox.cgi", content=b"x")
        assert len(mock.calls) == 0


def test_comando_sondear(monkeypatch, capsys, tmp_path):
    from idcmd_agente.cli import main

    monkeypatch.setattr("getpass.getpass", lambda _: "Clave-Lectura-2026")
    with respx.mock() as mock:
        _mock_dahua(mock)
        codigo = main(
            [
                "sondear",
                "--adaptador",
                "dahua_acceso",
                "--direccion",
                IP,
                "--usuario",
                "monitor",
                "--capturar",
                str(tmp_path / "captura"),
            ]
        )
    salida = capsys.readouterr().out
    assert codigo == 0
    assert '"modelo": "ASC1204C-D"' in salida
    assert "Clave-Lectura-2026" not in salida
    capturas = sorted(p.name for p in (tmp_path / "captura").iterdir())
    assert len(capturas) == 4 and all(".200.txt" in c for c in capturas)


# --- Respuestas reales: DS-K2602T de la oficina (2026-10-27) ----------------------------

K2602T = "hikvision_acceso/ds-k2602t_v1.1.1"
MOMENTO_CAPTURA = datetime(2026, 10, 27, 17, 13, 8, 900000, tzinfo=UTC)  # 12:13:08,9 Bogotá


async def _hikvision_real(**parametros):
    with respx.mock() as mock:
        mock.get(f"{BASE}/ISAPI/System/deviceInfo").respond(
            200, content=_leer(f"{K2602T}/deviceInfo.xml")
        )
        mock.get(f"{BASE}/ISAPI/System/time").respond(200, content=_leer(f"{K2602T}/time.xml"))
        mock.get(f"{BASE}/ISAPI/AccessControl/AcsWorkStatus").respond(
            200, content=_leer(f"{K2602T}/AcsWorkStatus.json")
        )
        async with httpx.AsyncClient() as cliente:
            adaptador = AdaptadorHikvisionAcceso(
                cliente_http=cliente, reloj=RelojFalso(MOMENTO_CAPTURA)
            )
            [r] = await adaptador.chequear(_objetivo("hikvision_acceso", **parametros), CRED)
    return r


async def test_panel_real_reporta_sus_cuatro_hallazgos():
    r = await _hikvision_real(lectoras_esperadas=2)
    assert (r.estado, r.codigo) == (Estado.DEGRADADO, "tamper_activo")  # el más grave
    m = r.metricas
    assert m["modelo"] == "DS-K2602T" and m["firmware"] == "V1.1.1 build 20230916"
    assert m["tamper_activo"] is True
    assert m["bateria_baja"] is True
    assert m["lectoras_desconectadas"] == 1
    # El reloj de pared está bien (−1,9 s); lo que está mal es la zona horaria.
    assert m["desfase_reloj_s"] == -1.9
    assert m["zona_horaria_utc_h"] == 0.0
    assert m["ntp_activo"] is False


async def test_zona_horaria_incorrecta_no_se_confunde_con_reloj_desfasado():
    import json as _json

    acs = _json.loads(_leer(f"{K2602T}/AcsWorkStatus.json"))
    acs["AcsWorkStatus"].update(hostAntiDismantleStatus="close", batteryLowVoltage=False)
    with respx.mock() as mock:
        mock.get(f"{BASE}/ISAPI/System/deviceInfo").respond(
            200, content=_leer(f"{K2602T}/deviceInfo.xml")
        )
        mock.get(f"{BASE}/ISAPI/System/time").respond(200, content=_leer(f"{K2602T}/time.xml"))
        mock.get(f"{BASE}/ISAPI/AccessControl/AcsWorkStatus").respond(200, json=acs)
        async with httpx.AsyncClient() as cliente:
            adaptador = AdaptadorHikvisionAcceso(
                cliente_http=cliente, reloj=RelojFalso(MOMENTO_CAPTURA)
            )
            [r] = await adaptador.chequear(_objetivo("hikvision_acceso"), CRED)
    assert (r.estado, r.codigo) == (Estado.DEGRADADO, "zona_horaria_incorrecta")

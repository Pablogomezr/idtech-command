"""NVR Dahua: salud del grabador, estado de cámaras por canal y retención real.

Con T0 = 2026-11-02 08:00 UTC (03:00 en Bogotá) y la grabación más antigua el
2026-10-03 08:00 hora de Bogotá, la retención real es 29,8 días.
"""

import uuid
from pathlib import Path

import httpx
import pytest
import respx
from conftest import T0, RelojFalso
from pydantic import SecretStr, ValidationError

from idcmd_agente.adaptadores.dahua_nvr import (
    OPS_NVR,
    OPS_RETENCION,
    AdaptadorDahuaNvr,
    AdaptadorDahuaRetencion,
)
from idcmd_agente.adaptadores.http_seguro import ClienteSoloLectura
from idcmd_agente.boveda import Credencial
from idcmd_agente.config import ConfigAgente
from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Objetivo
from idcmd_agente.nucleo.planificador import Planificador

FIX = Path(__file__).parent / "fixtures"
IP = "192.168.1.108"
BASE = f"http://{IP}"
CRED = Credencial(usuario="monitor", clave=SecretStr("Clave-Lectura-2026"))
NVR = uuid.UUID("88888888-8888-4888-8888-888888888888")
CAM_1 = uuid.UUID("99999999-9999-4999-8999-999999999991")
CAM_2 = uuid.UUID("99999999-9999-4999-8999-999999999992")


def _leer(nombre):
    return (FIX / nombre).read_bytes()


def _objetivo(adaptador="dahua_nvr", **parametros):
    return Objetivo(
        dispositivo_id=NVR,
        adaptador=adaptador,
        direccion=IP,
        canales={1: CAM_1, 2: CAM_2},
        parametros=parametros,
    )


def _mock_identidad(mock):
    mock.get(f"{BASE}/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"}).respond(
        200, content=_leer("dahua_nvr/getDeviceType.txt")
    )
    for accion in ["getSoftwareVersion", "getSerialNo"]:
        mock.get(f"{BASE}/cgi-bin/magicBox.cgi", params={"action": accion}).respond(
            200, content=_leer(f"dahua_acceso/{accion}.txt")
        )
    mock.get(f"{BASE}/cgi-bin/global.cgi", params={"action": "getCurrentTime"}).respond(
        200, content=_leer("dahua_acceso/getCurrentTime.txt")
    )


def _mock_nvr(mock, discos=None, camaras_status=200):
    _mock_identidad(mock)
    mock.get(f"{BASE}/cgi-bin/storageDevice.cgi", params={"action": "getDeviceAllInfo"}).respond(
        200, content=_leer("dahua_nvr/getDeviceAllInfo.txt") if discos is None else discos
    )
    mock.post(f"{BASE}/cgi-bin/api/LogicDeviceManager/getCameraState").respond(
        camaras_status, content=_leer("dahua_nvr/getCameraState.json")
    )


async def _correr(clase, objetivo, credencial=CRED):
    async with httpx.AsyncClient() as cliente:
        return await clase(cliente_http=cliente, reloj=RelojFalso(T0)).chequear(
            objetivo, credencial
        )


def _por_dispositivo(resultados):
    return {r.dispositivo_id: r for r in resultados}


# --- Salud del NVR y cámaras --------------------------------------------------------------


async def test_nvr_sano_y_estado_de_cada_camara():
    with respx.mock() as mock:
        _mock_nvr(mock)
        r = _por_dispositivo(await _correr(AdaptadorDahuaNvr, _objetivo()))
    assert (r[NVR].estado, r[NVR].codigo) == (Estado.OK, None)
    assert r[NVR].metricas["modelo"] == "DHI-NVR4108HS-4KS2"
    assert r[NVR].metricas["hdd_estado"] == "normal"
    assert r[NVR].metricas["almacenamiento_uso_pct"] == 94.0
    assert r[NVR].metricas["discos_con_error"] == 0
    assert (r[CAM_1].chequeo, r[CAM_1].estado) == ("dahua_nvr_canal", Estado.OK)
    assert (r[CAM_2].estado, r[CAM_2].codigo) == (Estado.CAIDO, "camara_desconectada_nvr")


async def test_disco_con_error():
    discos = _leer("dahua_nvr/getDeviceAllInfo.txt").replace(b"IsError=false", b"IsError=true")
    with respx.mock() as mock:
        _mock_nvr(mock, discos=discos)
        r = _por_dispositivo(await _correr(AdaptadorDahuaNvr, _objetivo()))
    assert (r[NVR].estado, r[NVR].codigo) == (Estado.DEGRADADO, "disco_con_error")
    assert r[NVR].metricas["discos_con_error"] == 1


async def test_nvr_sin_disco():
    with respx.mock() as mock:
        _mock_nvr(mock, discos=b"")
        r = _por_dispositivo(await _correr(AdaptadorDahuaNvr, _objetivo()))
    assert (r[NVR].estado, r[NVR].codigo) == (Estado.DEGRADADO, "sin_disco")


async def test_firmware_sin_getcamerastate_solo_informa_el_nvr():
    with respx.mock() as mock:
        _mock_nvr(mock, camaras_status=404)
        resultados = await _correr(AdaptadorDahuaNvr, _objetivo())
    assert [r.dispositivo_id for r in resultados] == [NVR]


async def test_nvr_caido():
    with respx.mock() as mock:
        mock.get(f"{BASE}/cgi-bin/magicBox.cgi").mock(side_effect=httpx.ConnectError("x"))
        [r] = await _correr(AdaptadorDahuaNvr, _objetivo())
    assert (r.estado, r.codigo) == (Estado.CAIDO, "sin_respuesta_http")


async def test_planificador_registra_eventos_de_las_camaras_del_nvr(tmp_path):
    """El NVR reporta a nombre de sus cámaras y el núcleo lo acepta sin conocer la marca."""
    buzon = Buzon(tmp_path / "b.sqlite3")
    with respx.mock() as mock:
        _mock_nvr(mock)
        async with httpx.AsyncClient() as cliente:
            plan = Planificador(
                [_objetivo()],
                None,
                MaquinaEstados(),
                buzon,
                Agregador(sesion=uuid.uuid4()),
                lambda: None,
                reloj=lambda: T0,
                adaptadores={"dahua_nvr": _ConCredencial(cliente)},
            )
            for _ in range(3):
                await plan.chequear(_objetivo())
    camaras = {(d, e) for d, _, e, _ in buzon.estados() if d in (CAM_1, CAM_2)}
    assert camaras == {(CAM_1, Estado.OK), (CAM_2, Estado.CAIDO)}


class _ConCredencial:
    """Envuelve el adaptador para la prueba (el planificador no tiene bóveda aquí)."""

    capacidades = frozenset()

    def __init__(self, cliente):
        self._a = AdaptadorDahuaNvr(cliente_http=cliente, reloj=RelojFalso(T0))

    async def chequear(self, objetivo, credencial):
        return await self._a.chequear(objetivo, CRED)


# --- Retención real --------------------------------------------------------------------------


def _mock_retencion(mock, siguiente=None, crear_status=200):
    ruta = f"{BASE}/cgi-bin/mediaFileFind.cgi"
    mock.get(ruta, params={"action": "factory.create"}).respond(
        crear_status, content=_leer("dahua_nvr/factory_create.txt")
    )
    mock.get(ruta, params={"action": "findFile"}).respond(
        200, content=_leer("dahua_nvr/findFile.txt")
    )
    mock.get(ruta, params={"action": "findNextFile"}).respond(
        200, content=siguiente if siguiente is not None else _leer("dahua_nvr/findNextFile.txt")
    )
    cerrar = mock.get(ruta, params={"action": "close"}).respond(200, content=b"OK")
    destruir = mock.get(ruta, params={"action": "destroy"}).respond(200, content=b"OK")
    return cerrar, destruir


@pytest.mark.parametrize(
    ("contratada", "estado", "codigo"),
    [(30, Estado.DEGRADADO, "retencion_insuficiente"), (25, Estado.OK, None)],
)
async def test_retencion_real_frente_a_contratada(contratada, estado, codigo):
    with respx.mock() as mock:
        cerrar, destruir = _mock_retencion(mock)
        r = _por_dispositivo(
            await _correr(
                AdaptadorDahuaRetencion,
                _objetivo("dahua_retencion", retencion_contratada_dias=contratada),
            )
        )
        url_busqueda = next(
            str(c.request.url) for c in mock.calls if "findFile&" in str(c.request.url)
        )
    assert r[CAM_1].metricas["retencion_real_dias"] == 29.8
    assert (r[CAM_1].estado, r[CAM_1].codigo) == (estado, codigo)
    assert r[CAM_1].chequeo == "dahua_retencion"
    # La búsqueda se cierra siempre, para no agotar los objetos del NVR.
    assert cerrar.call_count == destruir.call_count == 2
    # Los corchetes viajan sin codificar (algunos firmwares rechazan %5B0%5D).
    assert "condition.Types[0]=dav" in url_busqueda


async def test_canal_sin_grabaciones():
    with respx.mock() as mock:
        _mock_retencion(mock, siguiente=b"found=0\r\n")
        r = _por_dispositivo(await _correr(AdaptadorDahuaRetencion, _objetivo("dahua_retencion")))
    assert (r[CAM_1].estado, r[CAM_1].codigo) == (Estado.DEGRADADO, "sin_grabaciones")


async def test_firmware_sin_busqueda():
    with respx.mock(assert_all_called=False) as mock:
        _mock_retencion(mock, crear_status=400)
        r = _por_dispositivo(await _correr(AdaptadorDahuaRetencion, _objetivo("dahua_retencion")))
    assert (r[CAM_1].estado, r[CAM_1].codigo) == (Estado.DESCONOCIDO, "busqueda_no_soportada")


def test_retencion_no_cuenta_para_la_deteccion_de_5_minutos():
    base = {
        "agente_id": str(uuid.uuid4()),
        "central": {"url": "https://central.prueba", "archivo_token": "/run/secrets/t"},
    }
    objetivo = {
        "dispositivo_id": str(NVR),
        "adaptador": "dahua_retencion",
        "direccion": IP,
        "intervalo_s": 900,
        "canales": {"1": str(CAM_1)},
    }
    ConfigAgente.model_validate({**base, "objetivos": [{**objetivo, "deteccion": False}]})
    with pytest.raises(ValidationError, match="detección"):
        ConfigAgente.model_validate({**base, "objetivos": [objetivo]})


# --- Solo lectura ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "ruta", "params"),
    [
        # Devuelve las contraseñas de las cámaras en texto plano.
        ("GET", "/cgi-bin/configManager.cgi", {"action": "getConfig", "name": "RemoteDevice"}),
        ("GET", "/cgi-bin/configManager.cgi", {"action": "getConfig", "name": "Encode"}),
        ("GET", "/cgi-bin/magicBox.cgi", {"action": "reboot"}),
        ("GET", "/cgi-bin/snapshot.cgi", {"channel": "1"}),  # imagen: fuera de alcance
        ("GET", "/cgi-bin/RPC_Loadfile/mnt/dvr/2026-10-03/001/dav/08/08.00.00-09.00.00.dav", {}),
        ("GET", "/cgi-bin/playBack.cgi", {"action": "getStream"}),
        ("GET", "/cgi-bin/storageDevice.cgi", {"action": "formatPartition"}),
        (
            "GET",
            "/cgi-bin/mediaFileFind.cgi",
            {"action": "findNextFile", "object": "1", "count": "100"},
        ),
        ("POST", "/cgi-bin/api/LogicDeviceManager/setCameraConfig", {}),
    ],
)
def test_nvr_lista_blanca_bloquea(metodo, ruta, params):
    for ops in (OPS_NVR, OPS_RETENCION):
        assert not ClienteSoloLectura(BASE, ops).permitida(metodo, ruta, params)


def test_sondear_nvr_con_canales(monkeypatch, capsys):
    from idcmd_agente.cli import main

    monkeypatch.setattr("getpass.getpass", lambda _: "Clave-Lectura-2026")
    with respx.mock() as mock:
        _mock_nvr(mock)
        assert (
            main(
                [
                    "sondear",
                    "--adaptador",
                    "dahua_nvr",
                    "--direccion",
                    IP,
                    "--usuario",
                    "monitor",
                    "--canales",
                    "1-2",
                ]
            )
            == 0
        )
    salida = capsys.readouterr().out
    assert '"de": "equipo"' in salida and '"de": "canal 2"' in salida
    assert "camara_desconectada_nvr" in salida


def test_sondear_con_carpeta_de_captura_no_escribible(capsys, tmp_path):
    from idcmd_agente.cli import main

    # Un archivo donde debería ir una carpeta: falla incluso ejecutando como root.
    (tmp_path / "archivo").write_text("x")
    codigo = main(
        [
            "sondear",
            "--adaptador",
            "dahua_nvr",
            "--direccion",
            IP,
            "--capturar",
            str(tmp_path / "archivo" / "captura"),
        ]
    )
    assert codigo == 2
    assert "no se puede escribir" in capsys.readouterr().err

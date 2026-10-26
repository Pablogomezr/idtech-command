import uuid
from datetime import timedelta

import httpx
import pytest
import respx
from central_falsa import URL, CentralFalsa
from conftest import T0

from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.enviador import ConfigEnvio, Enviador, ErrorTemporal, TokenRevocado
from idcmd_agente.nucleo.modelos import Estado, EventoEstado, Latido

AGENTE = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _latido():
    return Latido(
        agente_version="0.1.0", reloj_agente=T0, inicio_sesion=T0, pendientes=0, adaptadores=[]
    )


def _llenar(buzon, dispositivo, n):
    for i in range(n):
        buzon.registrar_transicion(
            EventoEstado(
                dispositivo_id=dispositivo,
                chequeo="icmp",
                estado_anterior=Estado.OK,
                estado_nuevo=Estado.CAIDO,
                observado_at=T0 + timedelta(minutes=i),
            ),
            T0,
        )


@pytest.fixture
def central():
    falsa = CentralFalsa()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{URL}/api/v1/ingesta").mock(side_effect=falsa)
        yield falsa


def _enviador(buzon, **kwargs):
    config = ConfigEnvio(url_central=URL, token="tok-agente-123", agente_id=AGENTE, **kwargs)
    return Enviador(config, buzon, _latido, cliente=httpx.AsyncClient(), reloj=lambda: T0)


async def test_envia_en_lotes_con_gzip_y_token(tmp_path, dispositivo, central):
    buzon = Buzon(tmp_path / "b.sqlite3")
    _llenar(buzon, dispositivo, 7)
    enviados = await _enviador(buzon, tamano_lote=3).enviar_pendientes()
    assert enviados == 7
    assert [len(lote.eventos) for lote in central.lotes] == [3, 3, 1]
    assert central.cabeceras[0]["authorization"] == "Bearer tok-agente-123"
    assert central.cabeceras[0]["content-encoding"] == "gzip"
    assert buzon.pendientes() == 0


async def test_sin_items_igual_envia_latido(tmp_path, central):
    await _enviador(Buzon(tmp_path / "b.sqlite3")).enviar_pendientes()
    assert len(central.lotes) == 1 and central.lotes[0].latido.agente_version == "0.1.0"


async def test_sin_internet_no_marca_nada(tmp_path, dispositivo, central):
    buzon = Buzon(tmp_path / "b.sqlite3")
    _llenar(buzon, dispositivo, 2)
    central.en_linea = False
    with pytest.raises(httpx.TransportError):
        await _enviador(buzon).enviar_pendientes()
    assert buzon.pendientes() == 2


async def test_token_revocado(tmp_path, dispositivo, central):
    buzon = Buzon(tmp_path / "b.sqlite3")
    _llenar(buzon, dispositivo, 1)
    central.estado_http = 401
    with pytest.raises(TokenRevocado):
        await _enviador(buzon).enviar_pendientes()
    assert buzon.pendientes() == 1


async def test_lote_demasiado_grande_reduce_tamano(tmp_path, dispositivo, central):
    buzon = Buzon(tmp_path / "b.sqlite3")
    _llenar(buzon, dispositivo, 4)
    enviador = _enviador(buzon, tamano_lote=4)
    central.estado_http = 413
    with pytest.raises(ErrorTemporal):
        await enviador.enviar_pendientes()
    central.estado_http = 200
    await enviador.enviar_pendientes()
    assert [len(lote.eventos) for lote in central.lotes] == [2, 2]


def test_exige_https():
    with pytest.raises(ValueError):
        ConfigEnvio(url_central="http://central.idtech.com.co", token="t", agente_id=AGENTE)
    ConfigEnvio(url_central="http://localhost:8000", token="t", agente_id=AGENTE)

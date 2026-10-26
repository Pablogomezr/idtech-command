import uuid
from dataclasses import dataclass

import httpx
import icmplib
import pytest
import respx
from conftest import T0, RelojFalso

from idcmd_agente.adaptadores.http_seguro import ClienteSoloLectura, OperacionNoPermitida
from idcmd_agente.adaptadores.icmp import AdaptadorIcmp
from idcmd_agente.adaptadores.simulado import AdaptadorSimulado
from idcmd_agente.nucleo.modelos import Estado, Objetivo


@dataclass
class HostFalso:
    is_alive: bool
    packet_loss: float
    avg_rtt: float = 0.0


def _ping(host=None, error=None):
    async def ping(direccion, **kwargs):
        assert kwargs["privileged"] is False
        if error:
            raise error
        return host

    return ping


def _objetivo(adaptador="icmp", **parametros):
    return Objetivo(
        dispositivo_id=uuid.uuid4(),
        adaptador=adaptador,
        direccion="10.0.0.10",
        parametros=parametros,
    )


@pytest.mark.parametrize(
    ("host", "estado", "codigo"),
    [
        (HostFalso(True, 0.0, 2.345), Estado.OK, None),
        (HostFalso(True, 2 / 3, 4.0), Estado.DEGRADADO, "perdida_paquetes"),
        (HostFalso(False, 1.0), Estado.CAIDO, "sin_respuesta_icmp"),
    ],
)
async def test_icmp(host, estado, codigo):
    [r] = await AdaptadorIcmp(ping=_ping(host)).chequear(_objetivo(), None)
    assert (r.estado, r.codigo) == (estado, codigo)
    if estado == Estado.OK:
        assert r.metricas == {"latencia_ms": 2.35, "perdida_pct": 0.0}


async def test_icmp_sin_permiso_es_desconocido():
    adaptador = AdaptadorIcmp(ping=_ping(error=icmplib.SocketPermissionError(False)))
    [r] = await adaptador.chequear(_objetivo(), None)
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "sin_permiso_icmp")


async def test_simulado_es_reproducible_y_genera_caidas():
    objetivo = _objetivo("simulado", prob_caida=0.2, duracion_caida_s=300, semilla=7)

    async def correr():
        reloj = RelojFalso(T0)
        adaptador = AdaptadorSimulado(reloj=reloj)
        estados = []
        for _ in range(200):
            [r] = await adaptador.chequear(objetivo, None)
            estados.append(r.estado)
            reloj.avanzar(60)
        return estados

    primera, segunda = await correr(), await correr()
    assert primera == segunda
    assert Estado.CAIDO in primera and Estado.OK in primera


async def test_simulado_reporta_retencion():
    adaptador = AdaptadorSimulado(reloj=RelojFalso(T0))
    objetivo = _objetivo("simulado", prob_caida=0.0, retencion_contratada_dias=30)
    [r] = await adaptador.chequear(objetivo, None)
    real = r.metricas["retencion_real_dias"]
    assert r.estado == (Estado.DEGRADADO if real < 30 else Estado.OK)


async def test_cliente_solo_lectura_bloquea_fuera_de_lista_blanca():
    """Base del criterio 'BioStar sin marcaciones' y del principio de solo lectura."""
    with respx.mock() as mock:
        dispositivos = mock.get("https://biostar.local/api/devices").respond(200, json={})
        cliente = ClienteSoloLectura(
            "https://biostar.local",
            permitidas=[("POST", r"/api/login"), ("GET", r"/api/devices(/\d+)?")],
            cliente=httpx.AsyncClient(),
        )
        await cliente.solicitar("GET", "/api/devices")
        assert dispositivos.called
        for metodo, ruta in [
            ("GET", "/api/events"),
            ("POST", "/api/events/search"),
            ("PUT", "/api/devices/1"),
            ("GET", "/api/devices/1/../../events"),
        ]:
            with pytest.raises(OperacionNoPermitida):
                await cliente.solicitar(metodo, ruta)
        assert len(mock.calls) == 1

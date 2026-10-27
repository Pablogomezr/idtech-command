"""Instantáneas: valor actual de las métricas y auditoría de cambios de configuración."""

import uuid
from datetime import timedelta

import pytest
from test_ingesta import T0, _enviar, _lote


@pytest.fixture
async def cliente(bd):
    import httpx

    from idcmd_central.app import crear_app

    transporte = httpx.ASGITransport(app=crear_app(bd))
    async with httpx.AsyncClient(transport=transporte, base_url="http://central") as c:
        yield c


def _inst(dispositivo, minutos=0, **metricas):
    return {
        "id": str(uuid.uuid4()),
        "dispositivo_id": str(dispositivo),
        "chequeo": "hikvision_acceso",
        "estado": "ok",
        "observado_at": (T0 + timedelta(minutes=minutos)).isoformat(),
        "metricas": metricas,
    }


async def _mandar(cliente, datos, *instantaneas):
    lote = _lote(datos["agente_sitio_a1"])
    lote["instantaneas"] = list(instantaneas)
    r = await _enviar(cliente, datos.tokens["sitio_a1"], lote)
    assert r.status_code == 200
    return r.json()


async def test_cambio_de_usuarios_queda_auditado(cliente, datos, dueno):
    panel = datos["camara_sitio_a1"]
    await _mandar(cliente, datos, _inst(panel, 0, usuarios_registrados=12, latencia_ms=3.1))
    # Solo cambia una medición volátil: no es un cambio de configuración.
    r = await _mandar(cliente, datos, _inst(panel, 60, usuarios_registrados=12, latencia_ms=8.0))
    assert r["cambios"] == 0
    r = await _mandar(cliente, datos, _inst(panel, 61, usuarios_registrados=13, latencia_ms=3.0))
    assert r["cambios"] == 1

    antes, despues = dueno.execute(
        "SELECT antes, despues FROM idcmd.cambios_metricas WHERE dispositivo_id = %s", (panel,)
    ).fetchone()
    assert (antes, despues) == ({"usuarios_registrados": 12}, {"usuarios_registrados": 13})
    (actual,) = dueno.execute(
        "SELECT metricas FROM idcmd.metricas_actuales WHERE dispositivo_id = %s", (panel,)
    ).fetchone()
    assert actual["usuarios_registrados"] == 13


async def test_instantanea_atrasada_no_pisa_la_vigente(cliente, datos, dueno):
    panel = datos["camara_sitio_a1"]
    await _mandar(cliente, datos, _inst(panel, 30, usuarios_registrados=13))
    r = await _mandar(cliente, datos, _inst(panel, 10, usuarios_registrados=12))
    assert r["cambios"] == 0
    (actual,) = dueno.execute(
        "SELECT metricas FROM idcmd.metricas_actuales WHERE dispositivo_id = %s", (panel,)
    ).fetchone()
    assert actual["usuarios_registrados"] == 13


async def test_reenviar_no_duplica_el_cambio(cliente, datos, dueno):
    panel = datos["camara_sitio_a1"]
    await _mandar(cliente, datos, _inst(panel, 0, modos_verificacion="1:7,3:3"))
    cambio = _inst(panel, 5, modos_verificacion="1:3,3:3")  # bajó la seguridad de la lectora 1
    await _mandar(cliente, datos, cambio)
    await _mandar(cliente, datos, cambio)
    (n,) = dueno.execute("SELECT count(*) FROM idcmd.cambios_metricas").fetchone()
    assert n == 1


async def test_cliente_ve_solo_los_cambios_de_su_tenant(cliente, datos, bd):
    panel = datos["camara_sitio_a1"]
    await _mandar(cliente, datos, _inst(panel, 0, usuarios_registrados=1))
    await _mandar(cliente, datos, _inst(panel, 1, usuarios_registrados=2))
    for usuario, esperado in [("u_cliente_a", 1), ("u_cliente_b", 0), ("u_tecnico_1", 1)]:
        async with bd.como_usuario(datos[usuario]) as con:
            cur = await con.execute("SELECT count(*) FROM idcmd.cambios_metricas")
            assert (await cur.fetchone())[0] == esperado, usuario


async def test_instantanea_de_otro_sitio_se_rechaza(cliente, datos, dueno):
    r = await _mandar(cliente, datos, _inst(datos["camara_sitio_b1"], 0, usuarios_registrados=5))
    assert r["rechazados"] == 1
    (n,) = dueno.execute("SELECT count(*) FROM idcmd.metricas_actuales").fetchone()
    assert n == 0

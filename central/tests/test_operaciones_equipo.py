"""Auditoría de accesos administrativos a los equipos (ADR 0009)."""

import uuid
from datetime import timedelta

import psycopg
import pytest
from test_ingesta import T0, _enviar, _lote
from test_instantaneas import cliente  # noqa: F401  (fixture)


def _op(dispositivo, tipo="inicio_sesion", minutos=0, **extra):
    return {
        "id": str(uuid.uuid4()),
        "dispositivo_id": str(dispositivo),
        "ocurrido_at": (T0 + timedelta(minutes=minutos)).isoformat(),
        "tipo": tipo,
        "cuenta": "admin",
        "ip_origen": "192.168.1.50",
        "codigo_fabricante": "Login",
        **extra,
    }


async def _mandar(cliente, datos, *ops):  # noqa: F811
    lote = _lote(datos["agente_sitio_a1"])
    lote["operaciones"] = list(ops)
    return await _enviar(cliente, datos.tokens["sitio_a1"], lote)


async def test_operaciones_se_guardan_sin_duplicar(cliente, datos, dueno):  # noqa: F811
    nvr = datos["camara_sitio_a1"]
    ops = [_op(nvr), _op(nvr, "borrado_registros", 3, codigo_fabricante="ClearLog")]
    for _ in range(2):
        r = await _mandar(cliente, datos, *ops)
        assert r.status_code == 200
    filas = dueno.execute(
        "SELECT tipo, cuenta, host(ip_origen) FROM idcmd.operaciones_equipo ORDER BY ocurrido_at"
    ).fetchall()
    assert filas == [
        ("inicio_sesion", "admin", "192.168.1.50"),
        ("borrado_registros", "admin", "192.168.1.50"),
    ]


async def test_operacion_de_otro_sitio_se_rechaza(cliente, datos, dueno):  # noqa: F811
    r = await _mandar(cliente, datos, _op(datos["camara_sitio_b1"]))
    assert r.json()["rechazados"] == 1
    assert dueno.execute("SELECT count(*) FROM idcmd.operaciones_equipo").fetchone() == (0,)


async def test_tipo_desconocido_es_lote_invalido(cliente, datos):  # noqa: F811
    r = await _mandar(cliente, datos, _op(datos["camara_sitio_a1"], "hackear"))
    assert r.status_code == 422


async def test_auditoria_visible_por_tenant_y_no_modificable(cliente, datos, bd):  # noqa: F811
    await _mandar(cliente, datos, _op(datos["camara_sitio_a1"]))
    for usuario, esperado in [
        ("u_cliente_a", 1),
        ("u_cliente_b", 0),
        ("u_tecnico_1", 1),
        ("u_admin", 1),
    ]:
        async with bd.como_usuario(datos[usuario]) as con:
            cur = await con.execute("SELECT count(*) FROM idcmd.operaciones_equipo")
            assert (await cur.fetchone())[0] == esperado, usuario
    for sentencia in [
        "UPDATE idcmd.operaciones_equipo SET cuenta = 'otro'",
        "DELETE FROM idcmd.operaciones_equipo",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with bd.como_usuario(datos["u_admin"]) as con:
                await con.execute(sentencia)

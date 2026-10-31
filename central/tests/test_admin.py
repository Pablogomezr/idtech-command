"""Administración desde el portal: solo el admin; sitios, usuarios con acceso, credenciales que
de verdad sirven para entrar, restablecer y desactivar."""

from __future__ import annotations

import html
import re

import httpx
import psycopg
import pytest

from idcmd_central.app import crear_app
from idcmd_central.portal.seguridad import (
    codigo_totp,
    generar_llave,
    generar_token_sesion,
    hash_sesion,
)

ORIGEN = {"origin": "http://portal.test"}


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")


@pytest.fixture
def portal(bd, dueno, datos):
    def cliente(usuario: str | None = None):
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=crear_app(bd)), base_url="http://portal.test"
        )
        if usuario:
            token = generar_token_sesion()
            dueno.execute(
                "INSERT INTO idcmd.sesiones (token_hash, usuario_id, expira_at)"
                " VALUES (%s, %s, now() + interval '1 hour')",
                (hash_sesion(token), datos[usuario]),
            )
            c.cookies.set("idcmd_sesion", token)
        return c

    return cliente


def _credenciales(pagina: str) -> tuple[str, str]:
    claves = re.findall(r'<code class="clave">([^<]+)</code>', pagina)
    return html.unescape(claves[0]), claves[1].replace(" ", "")


async def _entrar(portal, correo, clave, secreto) -> int:
    async with portal() as c:
        datos = {"correo": correo, "clave": clave, "codigo": codigo_totp(secreto)}
        r = await c.post("/portal/entrar", headers=ORIGEN, data=datos)
        return r.status_code


async def test_solo_el_administrador(portal):
    for quien in ("u_tecnico_1", "u_cliente_a"):
        async with portal(quien) as c:
            assert (await c.get("/portal/admin")).status_code == 404
            r = await c.post("/portal/admin/usuarios", headers=ORIGEN,
                             data={"nombre": "X", "correo": "x@x.co", "rol": "admin"})  # fmt: skip
            assert r.status_code == 404
    async with portal() as c:
        assert (await c.get("/portal/admin")).status_code == 303  # sin ingreso
    async with portal("u_admin") as c:
        assert (await c.get("/portal/admin")).status_code == 200
        r = await c.post("/portal/admin/usuarios", headers={"origin": "https://otro.sitio"},
                         data={"nombre": "X", "correo": "x@x.co", "rol": "admin"})  # fmt: skip
        assert r.status_code == 403


async def test_las_funciones_de_la_base_rechazan_a_quien_no_es_admin(bd, datos):
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await con.execute(
                "SELECT idcmd.crear_usuario_portal('a@b.co', 'A', 'admin', 'clave-muy-larga', NULL)"
            )


async def test_crear_sitio_con_cliente_nuevo_y_tecnico(portal, dueno, datos):
    async with portal("u_admin") as c:
        r = await c.post("/portal/admin/sitios", headers=ORIGEN, data={
            "cliente_nuevo": "Clínica Nueva", "regional": "Atlántico", "nombre": "Sede Norte",
            "ciudad": "Barranquilla", "lat": "10,99", "lon": "-74.79",
            "principal": str(datos["u_tecnico_1"]),
        })  # fmt: skip
        assert r.status_code == 303
        sitio = dueno.execute(
            "SELECT s.id, c.nombre, r.nombre, s.lat::float FROM idcmd.sitios s"
            " JOIN idcmd.clientes c ON c.id = s.cliente_id"
            " JOIN idcmd.regionales r ON r.id = s.regional_id WHERE s.nombre = 'Sede Norte'"
        ).fetchone()
        assert sitio[1:] == ("Clínica Nueva", "Atlántico", 10.99)
        tecnico = dueno.execute(
            "SELECT tecnico_id FROM idcmd.asignaciones_tecnico WHERE sitio_id = %s"
            " AND tipo = 'principal' AND vigente_hasta IS NULL",
            (sitio[0],),
        ).fetchone()[0]
        assert tecnico == datos["u_tecnico_1"]
        lista = await c.get("/portal/admin")
        assert "Sede Norte" in lista.text
        # Datos inválidos: se rechazan con el formulario y el mensaje, sin crear nada.
        r = await c.post("/portal/admin/sitios", headers=ORIGEN, data={
            "cliente_nuevo": "Otra", "nombre": "Mala", "lat": "abc", "lon": "1"})  # fmt: skip
        assert r.status_code == 400 and "Latitud y longitud" in r.text


async def test_tecnico_creado_desde_el_portal_entra_y_ve_su_sitio(portal, bd, dueno, datos):
    async with portal("u_admin") as c:
        r = await c.post("/portal/admin/usuarios", headers=ORIGEN, data={
            "nombre": "Albert Einstein", "correo": "albert@idtech.test", "rol": "tecnico",
            "sitios": str(datos["sitio_a2"])})  # fmt: skip
        assert r.status_code == 200 and "<svg" in r.text  # QR del segundo factor
        clave, secreto = _credenciales(r.text)
        # Sin sitio, un técnico no se crea.
        r2 = await c.post("/portal/admin/usuarios", headers=ORIGEN, data={
            "nombre": "Sin Sitio", "correo": "sin@idtech.test", "rol": "tecnico"})  # fmt: skip
        assert r2.status_code == 400 and "al menos un sitio" in r2.text
        r3 = await c.post("/portal/admin/usuarios", headers=ORIGEN, data={
            "nombre": "Repetido", "correo": "albert@idtech.test", "rol": "tecnico",
            "sitios": str(datos["sitio_a2"])})  # fmt: skip
        assert r3.status_code == 400 and "Ya existe" in r3.text
    assert await _entrar(portal, "albert@idtech.test", clave, secreto) == 303
    nuevo = dueno.execute(
        "SELECT id FROM idcmd.usuarios WHERE correo = 'albert@idtech.test'"
    ).fetchone()[0]
    async with bd.como_usuario(nuevo) as con:
        cur = await con.execute("SELECT nombre FROM idcmd.sitios")
        assert [n for (n,) in await cur.fetchall()] == ["sitio_a2"]


async def test_supervisor_del_contrato_ve_todo_su_cliente(portal, bd, dueno, datos):
    async with portal("u_admin") as c:
        r = await c.post("/portal/admin/usuarios", headers=ORIGEN, data={
            "nombre": "Supervisora", "correo": "sup@cliente.test", "rol": "cliente",
            "cliente_id": str(datos["cliente_a"])})  # fmt: skip
        clave, secreto = _credenciales(r.text)
    assert await _entrar(portal, "sup@cliente.test", clave, secreto) == 303
    sup = dueno.execute(
        "SELECT id FROM idcmd.usuarios WHERE correo = 'sup@cliente.test'"
    ).fetchone()
    async with bd.como_usuario(sup[0]) as con:
        cur = await con.execute("SELECT count(*) FROM idcmd.sitios")
        assert (await cur.fetchone())[0] == 3


async def test_restablecer_y_desactivar(portal, dueno, datos):
    async with portal("u_admin") as c:
        r = await c.post("/portal/admin/usuarios", headers=ORIGEN, data={
            "nombre": "Tec Dos", "correo": "dos@idtech.test", "rol": "tecnico",
            "sitios": str(datos["sitio_a1"])})  # fmt: skip
        vieja, secreto_viejo = _credenciales(r.text)
        uid = dueno.execute(
            "SELECT id FROM idcmd.usuarios WHERE correo = 'dos@idtech.test'"
        ).fetchone()[0]
        r = await c.post(f"/portal/admin/usuarios/{uid}/restablecer", headers=ORIGEN)
        nueva, secreto = _credenciales(r.text)
    assert await _entrar(portal, "dos@idtech.test", vieja, secreto_viejo) == 401
    assert await _entrar(portal, "dos@idtech.test", nueva, secreto) == 303
    async with portal("u_admin") as c:
        r = await c.post(
            f"/portal/admin/usuarios/{uid}/activo", headers=ORIGEN, data={"activo": "no"}
        )
        assert r.status_code == 303
        propio = await c.post(f"/portal/admin/usuarios/{datos['u_admin']}/activo", headers=ORIGEN,
                              data={"activo": "no"})  # fmt: skip
        assert "No%20puede%20desactivar" in propio.headers["location"]
    assert await _entrar(portal, "dos@idtech.test", nueva, secreto) == 401
    abiertas = dueno.execute(
        "SELECT count(*) FROM idcmd.sesiones WHERE usuario_id = %s AND cerrada_at IS NULL", (uid,)
    ).fetchone()[0]
    assert abiertas == 0
    auditoria = dueno.execute(
        "SELECT array_agg(DISTINCT accion) FROM idcmd.auditoria_accesos WHERE accion LIKE 'admin_%'"
    ).fetchone()[0]
    assert {"admin_usuario_crear", "admin_usuario_restablecer", "admin_usuario_activo"} <= set(
        auditoria
    )

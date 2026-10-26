"""Criterio de aceptación: un usuario Cliente solo ve su propio tenant (y técnicos solo sus
sitios). Todo se prueba contra Postgres real, con los roles reales de la aplicación."""

import uuid

import psycopg
import pytest

TABLAS_CON_SITIO = ["dispositivos", "agentes", "eventos_estado", "estado_actual", "agregados_hora"]


async def _ids(bd, usuario_id, tabla, columna="id"):
    async with bd.como_usuario(usuario_id) as con:
        cur = await con.execute(f"SELECT {columna} FROM idcmd.{tabla}")
        return {fila[0] for fila in await cur.fetchall()}


async def _sitios(bd, usuario_id):
    return await _ids(bd, usuario_id, "sitios")


async def test_sin_usuario_valido_no_ve_nada(bd, datos):
    desconocido = uuid.uuid4()
    for tabla in [
        "clientes",
        "regionales",
        "sitios",
        *TABLAS_CON_SITIO,
        "usuarios",
        "alcances_usuario",
        "asignaciones_tecnico",
        "auditoria_accesos",
    ]:
        columna = "dispositivo_id" if tabla == "estado_actual" else "id"
        assert await _ids(bd, desconocido, tabla, columna) == set(), tabla


async def test_cliente_a_ve_solo_su_tenant(bd, datos):
    u = datos["u_cliente_a"]
    assert await _sitios(bd, u) == {datos["sitio_a1"], datos["sitio_a2"], datos["sitio_a3"]}
    assert await _ids(bd, u, "clientes") == {datos["cliente_a"]}
    assert await _ids(bd, u, "regionales") == {datos["reg_a_norte"], datos["reg_a_sur"]}
    camaras_a = {datos[f"camara_sitio_a{i}"] for i in (1, 2, 3)}
    assert await _ids(bd, u, "dispositivos") == camaras_a
    assert await _ids(bd, u, "eventos_estado", "dispositivo_id") == camaras_a


async def test_cliente_b_no_ve_nada_de_a(bd, datos):
    u = datos["u_cliente_b"]
    assert await _sitios(bd, u) == {datos["sitio_b1"]}
    assert await _ids(bd, u, "clientes") == {datos["cliente_b"]}
    for tabla in TABLAS_CON_SITIO:
        async with bd.como_usuario(u) as con:
            cur = await con.execute(
                f"SELECT count(*) FROM idcmd.{tabla} WHERE cliente_id = %s", (datos["cliente_a"],)
            )
            assert (await cur.fetchone())[0] == 0, tabla


async def test_alcance_por_regional_y_por_sitio(bd, datos):
    assert await _sitios(bd, datos["u_cliente_a_norte"]) == {datos["sitio_a1"], datos["sitio_a2"]}
    assert await _sitios(bd, datos["u_cliente_a3"]) == {datos["sitio_a3"]}
    assert await _ids(bd, datos["u_cliente_a3"], "regionales") == {datos["reg_a_sur"]}


async def test_usuario_inactivo_no_ve_nada(bd, datos):
    assert await _sitios(bd, datos["u_cliente_inactivo"]) == set()


async def test_tecnico_ve_solo_sitios_asignados_y_vigentes(bd, datos):
    assert await _sitios(bd, datos["u_tecnico_1"]) == {datos["sitio_a1"]}
    assert await _ids(bd, datos["u_tecnico_1"], "dispositivos") == {datos["camara_sitio_a1"]}
    # La asignación del suplente venció ayer.
    assert await _sitios(bd, datos["u_tecnico_vencido"]) == set()


async def test_reasignar_cambia_lo_que_ve_el_tecnico(bd, datos):
    async with bd.como_usuario(datos["u_admin"]) as con:
        await con.execute(
            "UPDATE idcmd.asignaciones_tecnico SET vigente_hasta = now() + interval '1 second'"
            " WHERE tecnico_id = %s",
            (datos["u_tecnico_1"],),
        )
        await con.execute(
            "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id, tipo)"
            " VALUES (%s, %s, %s, 'principal')",
            (datos["u_tecnico_1"], datos["cliente_b"], datos["sitio_b1"]),
        )
    assert datos["sitio_b1"] in await _sitios(bd, datos["u_tecnico_1"])


async def test_admin_ve_todo(bd, datos):
    assert len(await _sitios(bd, datos["u_admin"])) == 4
    assert len(await _ids(bd, datos["u_admin"], "usuarios")) == 8


@pytest.mark.parametrize("usuario", ["u_cliente_a", "u_tecnico_1"])
async def test_cliente_y_tecnico_no_editan_inventario(bd, datos, usuario):
    u = datos[usuario]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with bd.como_usuario(u) as con:
            await con.execute(
                "INSERT INTO idcmd.dispositivos (cliente_id, sitio_id, nombre, tipo_equipo)"
                " VALUES (%s, %s, 'intruso', 'camara')",
                (datos["cliente_a"], datos["sitio_a1"]),
            )
    async with bd.como_usuario(u) as con:
        cur = await con.execute("UPDATE idcmd.dispositivos SET nombre = 'x'")
        assert cur.rowcount == 0
        cur = await con.execute("DELETE FROM idcmd.sitios")
        assert cur.rowcount == 0


async def test_portal_no_puede_escribir_historial_de_estado(bd, datos):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with bd.como_usuario(datos["u_admin"]) as con:
            await con.execute("DELETE FROM idcmd.eventos_estado")


async def test_admin_edita_inventario(bd, datos):
    async with bd.como_usuario(datos["u_admin"]) as con:
        cur = await con.execute(
            "UPDATE idcmd.dispositivos SET nombre = 'Cámara portería' WHERE id = %s",
            (datos["camara_sitio_b1"],),
        )
        assert cur.rowcount == 1


async def test_portal_no_lee_hashes_de_tokens_ni_claves(bd, datos):
    for consulta in [
        "SELECT token_hash FROM idcmd.agentes",
        "SELECT hash_clave FROM idcmd.usuarios",
        "SELECT totp_cifrado FROM idcmd.usuarios",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with bd.como_usuario(datos["u_admin"]) as con:
                await con.execute(consulta)


async def test_auditoria_solo_agrega(bd, datos):
    async with bd.como_usuario(datos["u_cliente_a"]) as con:
        await con.execute(
            "INSERT INTO idcmd.auditoria_accesos (usuario_id, accion, recurso)"
            " VALUES (%s, 'ver', 'sitio')",
            (datos["u_cliente_a"],),
        )
    # Nadie se hace pasar por otro en la auditoría.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with bd.como_usuario(datos["u_cliente_a"]) as con:
            await con.execute(
                "INSERT INTO idcmd.auditoria_accesos (usuario_id, accion) VALUES (%s, 'x')",
                (datos["u_admin"],),
            )
    for sentencia in [
        "UPDATE idcmd.auditoria_accesos SET accion = 'otra'",
        "DELETE FROM idcmd.auditoria_accesos",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with bd.como_usuario(datos["u_admin"]) as con:
                await con.execute(sentencia)
    assert len(await _ids(bd, datos["u_admin"], "auditoria_accesos")) == 1
    assert await _ids(bd, datos["u_cliente_a"], "auditoria_accesos") == set()


async def test_asignar_sitio_a_un_cliente_falla(bd, datos):
    with pytest.raises(psycopg.errors.RaiseException):
        async with bd.como_usuario(datos["u_admin"]) as con:
            await con.execute(
                "INSERT INTO idcmd.asignaciones_tecnico (tecnico_id, cliente_id, sitio_id)"
                " VALUES (%s, %s, %s)",
                (datos["u_cliente_a"], datos["cliente_a"], datos["sitio_a3"]),
            )


async def test_agente_no_escribe_en_otro_sitio(bd, datos):
    """Aunque el código de ingesta fallara, la base impide cruzar sitios."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with bd.como_agente(datos["agente_sitio_a1"]) as con:
            await con.execute(
                "INSERT INTO idcmd.eventos_estado (id, dispositivo_id, cliente_id, sitio_id,"
                " agente_id, chequeo, estado_anterior, estado_nuevo, observado_at)"
                " VALUES (%s, %s, %s, %s, %s, 'icmp', 'ok', 'caido', now())",
                (
                    uuid.uuid4(),
                    datos["camara_sitio_b1"],
                    datos["cliente_b"],
                    datos["sitio_b1"],
                    datos["agente_sitio_a1"],
                ),
            )
    async with bd.como_agente(datos["agente_sitio_a1"]) as con:
        cur = await con.execute("SELECT id FROM idcmd.dispositivos")
        assert {f[0] for f in await cur.fetchall()} == {datos["camara_sitio_a1"]}


def test_todas_las_tablas_tienen_rls(dueno, datos):
    """Si se agrega una tabla sin RLS, esta prueba falla."""
    sin_rls = dueno.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'idcmd' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity"
    ).fetchall()
    assert sin_rls == []


def test_roles_de_servicio_no_saltan_la_rls(dueno, datos):
    filas = dueno.execute(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles"
        " WHERE rolname IN ('idcmd_app', 'idcmd_ingesta', 'idcmd_servicio_pruebas')"
    ).fetchall()
    assert len(filas) == 3
    assert all(not superusuario and not bypass for _, superusuario, bypass in filas)
    duenos = dueno.execute(
        "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'idcmd'"
    ).fetchall()
    assert not {"idcmd_app", "idcmd_ingesta", "idcmd_servicio_pruebas"} & {d for (d,) in duenos}

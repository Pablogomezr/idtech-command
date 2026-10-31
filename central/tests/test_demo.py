"""Cliente demo: se crea completo, se puede repetir, solo ve lo suyo y nunca envía avisos."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import yaml

from idcmd_central import demo
from idcmd_central.cli import main
from idcmd_central.portal.seguridad import generar_llave
from idcmd_central.servicio import avisos


@pytest.fixture(autouse=True)
def entorno(base_pruebas, monkeypatch):
    monkeypatch.setenv("IDCMD_DSN_ADMIN", base_pruebas["dueno"])
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_URL_PUBLICA", "https://command.idtech.test")


async def _preparar(tmp_path):
    # La consola usa su propio ciclo asíncrono: como en el servidor, va en otro hilo.
    argumentos = ["demo", "preparar", "--correo", "demo@idtech.test", "--correo-tecnico",
                  "tecnico.demo@idtech.test", "--salida", str(tmp_path)]  # fmt: skip
    return await asyncio.to_thread(main, argumentos)


async def test_demo_completa_y_repetible(bd, dueno, datos, tmp_path, capsys):
    assert await _preparar(tmp_path) == 0
    salida = capsys.readouterr().out
    assert "clave del portal:" in salida and "informes emitidos: " in salida
    sitio = demo._id("sitio")
    tickets = dueno.execute(
        "SELECT count(*), count(*) FILTER (WHERE cierre = 'verificado') FROM idcmd.tickets"
        " WHERE sitio_id = %s",
        (sitio,),
    ).fetchone()
    assert tickets[0] >= 10 and tickets[1] >= 3
    informes = dueno.execute(
        "SELECT count(*), min(disponibilidad), max(disponibilidad) FROM idcmd.informes_mensuales"
        " WHERE sitio_id = %s",
        (sitio,),
    ).fetchone()
    assert informes[0] == 2 and 90 < informes[1] <= informes[2] < 100
    config = yaml.safe_load((tmp_path / "agente" / "agente.yaml").read_text())
    assert config["central"]["url"] == "http://localhost:8000"
    assert {o["adaptador"] for o in config["objetivos"]} == {"simulado"}
    assert (tmp_path / "agente" / "token_agente").exists() and (
        tmp_path / "secretos" / "llave"
    ).exists()

    # Repetir no duplica nada ni cambia el token del agente.
    token = (tmp_path / "agente" / "token_agente").read_text()
    assert await _preparar(tmp_path) == 0
    assert "ya existía" in capsys.readouterr().out
    assert (tmp_path / "agente" / "token_agente").read_text() == token
    assert (
        dueno.execute(
            "SELECT count(*) FROM idcmd.tickets WHERE sitio_id = %s", (sitio,)
        ).fetchone()[0]
        == tickets[0]
    )


async def test_el_usuario_demo_solo_ve_la_demo(bd, dueno, datos, tmp_path):
    await _preparar(tmp_path)
    usuario = dueno.execute(
        "SELECT id FROM idcmd.usuarios WHERE correo = 'demo@idtech.test'"
    ).fetchone()[0]
    async with bd.como_usuario(usuario) as con:
        cur = await con.execute("SELECT nombre FROM idcmd.sitios")
        assert [n for (n,) in await cur.fetchall()] == [demo.SITIO]
        cur = await con.execute("SELECT count(*) FROM idcmd.tickets")
        assert (await cur.fetchone())[0] > 0


async def test_la_demo_nunca_envia_avisos(bd, dueno, datos, tmp_path):
    await _preparar(tmp_path)
    async with bd.como_worker() as con:
        await avisos.generar(con, datetime.now(UTC))
    assert (
        dueno.execute(
            "SELECT count(*) FROM idcmd.notificaciones WHERE sitio_id = %s", (demo._id("sitio"),)
        ).fetchone()[0]
        == 0
    )


async def _portal(bd, dueno, correo):
    import httpx

    from idcmd_central.app import crear_app
    from idcmd_central.portal.seguridad import generar_token_sesion, hash_sesion

    token = generar_token_sesion()
    dueno.execute(
        "INSERT INTO idcmd.sesiones (token_hash, usuario_id, expira_at) SELECT %s, id,"
        " now() + interval '1 hour' FROM idcmd.usuarios WHERE correo = %s",
        (hash_sesion(token), correo),
    )
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crear_app(bd)), base_url="http://p.test"
    )
    c.cookies.set("idcmd_sesion", token)
    return c


async def test_reporte_de_servicios_por_equipo_y_excel(bd, dueno, datos, tmp_path, monkeypatch):
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")
    await _preparar(tmp_path)
    hoy = datetime.now(UTC)
    rango = f"desde={hoy.year - (hoy.month <= 2)}-{(hoy.month - 3) % 12 + 1:02d}&hasta={hoy:%Y-%m}"
    async with await _portal(bd, dueno, "demo@idtech.test") as c:
        r = await c.get(f"/portal/servicios?{rango}&por=equipo")
        assert r.status_code == 200 and "Cámara 3 · parqueadero" in r.text
        assert "Técnico</th>" not in r.text  # el cliente no ve la columna de técnicos
        excel = await c.get(f"/portal/servicios.csv?{rango}")
        texto = excel.content.decode("utf-8-sig")
        filas = texto.strip().splitlines()
        assert filas[0].startswith("Ticket;Sitio;Equipo") and len(filas) > 10
        assert "Sede Demo" in filas[1]
    async with await _portal(bd, dueno, "tecnico.demo@idtech.test") as c:
        r = await c.get(f"/portal/servicios?{rango}&por=tecnico")
        assert r.status_code == 200 and "Técnico Demo" in r.text
    async with await _portal(bd, dueno, "cliente_b@prueba.co") as c:  # otro cliente: nada
        r = await c.get(f"/portal/servicios.csv?{rango}")
        assert len(r.content.decode("utf-8-sig").strip().splitlines()) == 1


def test_clave_generada_y_teclas_especiales():
    """La tecla F7 trabada metía '^[[18~' en la clave escrita en la consola."""
    from idcmd_central.cli import generar_clave, limpiar_clave

    f7 = "\x1b[18~"
    assert limpiar_clave(f"Mi{f7 * 3}Clave{f7}Segura123\r") == "MiClaveSegura123"
    clave = generar_clave()
    assert len(clave) == 23 and clave.count("-") == 3
    assert not set(clave) & set("0O1lI")
    assert generar_clave() != clave


async def test_usuarios_tecnico_y_supervisor_con_acceso(bd, dueno, datos, capsys):
    """Técnico con su sitio y supervisor del contrato (cliente) con todo su cliente."""

    def consola(*argumentos):
        return main(list(argumentos))

    def con_sitios(correo):
        return dueno.execute(
            "SELECT id FROM idcmd.usuarios WHERE correo = %s", (correo,)
        ).fetchone()[0]

    assert await asyncio.to_thread(consola, "usuarios", "crear", "--correo", "tec@idtech.test",
        "--nombre", "Técnico Uno", "--rol", "tecnico", "--sitio", "sitio_a2",
        "--generar-clave") == 0  # fmt: skip
    assert await asyncio.to_thread(consola, "usuarios", "crear", "--correo", "sup@cliente.test",
        "--nombre", "Supervisora", "--rol", "cliente", "--cliente", "Integradora A",
        "--generar-clave") == 0  # fmt: skip
    salida = capsys.readouterr().out
    assert "acceso: sitio_a2: técnico principal" in salida
    assert "acceso: ve todos los sitios de Integradora A" in salida
    async with bd.como_usuario(con_sitios("tec@idtech.test")) as con:
        cur = await con.execute("SELECT nombre FROM idcmd.sitios")
        assert [n for (n,) in await cur.fetchall()] == ["sitio_a2"]
    async with bd.como_usuario(con_sitios("sup@cliente.test")) as con:
        cur = await con.execute("SELECT nombre FROM idcmd.sitios ORDER BY nombre")
        assert [n for (n,) in await cur.fetchall()] == ["sitio_a1", "sitio_a2", "sitio_a3"]
    # Sin sitio, un técnico no se crea (entraría y no vería nada).
    with pytest.raises(SystemExit):
        await asyncio.to_thread(consola, "usuarios", "crear", "--correo", "x@idtech.test",
                                "--nombre", "X", "--rol", "tecnico", "--generar-clave")  # fmt: skip
    assert await asyncio.to_thread(consola, "usuarios", "acceso", "--correo", "tec@idtech.test",
                                   "--sitio", "sitio_a3") == 0  # fmt: skip
    assert await asyncio.to_thread(consola, "usuarios", "listar") == 0
    assert "sitio_a2, sitio_a3" in capsys.readouterr().out


async def test_crear_usuario_que_ya_existe_no_muestra_clave(bd, dueno, datos, capsys):
    with pytest.raises(SystemExit) as fin:
        await asyncio.to_thread(main, ["usuarios", "crear", "--correo", "tecnico_1@prueba.co",
                                       "--nombre", "Otro", "--rol", "tecnico", "--sitio",
                                       "sitio_a1", "--generar-clave"])  # fmt: skip
    assert "ya existe" in str(fin.value) and "no se cambió nada" in str(fin.value)
    assert "Clave nueva del portal" not in capsys.readouterr().out


async def test_rendimiento_de_tecnicos_y_confiabilidad_de_equipos(bd, dueno, datos, tmp_path,
                                                                   monkeypatch):  # fmt: skip
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")
    await _preparar(tmp_path)
    hoy = datetime.now(UTC)
    rango = f"desde={hoy.year - (hoy.month <= 2)}-{(hoy.month - 3) % 12 + 1:02d}&hasta={hoy:%Y-%m}"
    async with await _portal(bd, dueno, "admin@prueba.co") as c:
        r = await c.get(f"/portal/servicios/tecnicos?{rango}")
        assert r.status_code == 200 and "Técnico Demo" in r.text and "Retrabajos" in r.text
        excel = (await c.get(f"/portal/servicios/tecnicos.csv?{rango}")).content
        filas = excel.decode("utf-8-sig").strip().splitlines()
        assert filas[0].startswith("Técnico;Servicios;Atendidos") and len(filas) >= 2
        r = await c.get(f"/portal/servicios/equipos?{rango}&sitio={demo._id('sitio')}")
        assert r.status_code == 200 and "Cámara 3 · parqueadero" in r.text
        excel = await c.get(f"/portal/servicios/equipos.csv?{rango}&sitio={demo._id('sitio')}")
        filas = excel.content.decode("utf-8-sig").strip().splitlines()
        assert filas[0].startswith("Sitio;Equipo;Tipo;Disponibilidad")
        camara = next(f.split(";") for f in filas if "Cámara 3" in f)
        assert int(camara[4]) >= 1  # la cámara del parqueadero se cae en la demo
    async with await _portal(bd, dueno, "demo@idtech.test") as c:
        assert (await c.get(f"/portal/servicios/tecnicos?{rango}")).status_code == 404
        r = await c.get(f"/portal/servicios/equipos?{rango}")
        assert r.status_code == 200 and "Cámara 3 · parqueadero" in r.text
        assert ">Técnicos<" not in r.text  # el cliente no ve la pestaña
    async with await _portal(bd, dueno, "tecnico.demo@idtech.test") as c:
        r = await c.get(f"/portal/servicios/tecnicos.csv?{rango}")
        filas = r.content.decode("utf-8-sig").strip().splitlines()
        assert [f.split(";")[0] for f in filas[1:]] == ["Técnico Demo"]  # solo su fila

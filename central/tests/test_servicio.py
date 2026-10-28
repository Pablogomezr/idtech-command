"""Módulo de servicio: del equipo caído al ticket cerrado y verificado, con avisos y SLA."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from idcmd_central.portal.seguridad import generar_llave
from idcmd_central.servicio import avisos
from idcmd_central.servicio.reglas import firma_seguimiento, prioridad
from idcmd_central.servicio.worker import ciclo

# Después de "ahora": las asignaciones del escenario empiezan a regir en el momento de sembrarlas.
T0 = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)


@pytest.fixture(autouse=True)
def llave(monkeypatch):
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_URL_PUBLICA", "https://command.idtech.test")
    for v in ("IDCMD_SMTP_SERVIDOR", "IDCMD_TELEGRAM_TOKEN"):
        monkeypatch.delenv(v, raising=False)


def _falla(dueno, datos, sitio="sitio_a1", codigo="sin_respuesta_icmp", estado="caido", desde=T0):
    dueno.execute(
        "INSERT INTO idcmd.estado_actual (dispositivo_id, chequeo, cliente_id, sitio_id, estado,"
        " desde, codigo) SELECT id, 'icmp', cliente_id, sitio_id, %s, %s, %s"
        " FROM idcmd.dispositivos WHERE id = %s"
        " ON CONFLICT (dispositivo_id, chequeo) DO UPDATE SET estado = excluded.estado,"
        " desde = excluded.desde, codigo = excluded.codigo",
        (estado, desde, codigo, datos[f"camara_{sitio}"]),
    )


def _recupera(dueno, datos, sitio="sitio_a1", desde=T0):
    _falla(dueno, datos, sitio, codigo=None, estado="ok", desde=desde)


def _ticket(dueno, datos, sitio="sitio_a1"):
    return dueno.execute(
        "SELECT estado::text, prioridad::text, tecnico_id, nivel_escalamiento, cierre,"
        " vence_respuesta_at, id, numero FROM idcmd.tickets WHERE sitio_id = %s"
        " ORDER BY numero DESC LIMIT 1",
        (datos[sitio],),
    ).fetchone()


def _eventos(dueno, ticket_id):
    return [
        t
        for (t,) in dueno.execute(
            "SELECT tipo FROM idcmd.eventos_ticket WHERE ticket_id = %s ORDER BY id", (ticket_id,)
        ).fetchall()
    ]


@pytest.fixture
def contacto(dueno, datos):
    dueno.execute(
        "INSERT INTO idcmd.contactos_sitio (cliente_id, sitio_id, nombre, correo)"
        " VALUES (%s, %s, 'Jefe de seguridad', 'seguridad@cliente-a.test')",
        (datos["cliente_a"], datos["sitio_a1"]),
    )


# --- Reglas -------------------------------------------------------------------------------------


def test_prioridades():
    assert prioridad("sin_respuesta_icmp", "camara", 2) == ("alta", "correctivo")
    assert prioridad("sin_respuesta_icmp", "nvr", 2) == ("critica", "correctivo")  # sin video
    assert prioridad("sin_respuesta_icmp", "camara", 1) == ("critica", "correctivo")
    assert prioridad("reloj_desfasado", "nvr", 1) == ("baja", "preventivo")


# --- Ciclo --------------------------------------------------------------------------------------


async def test_falla_abre_ticket_y_lo_asigna_al_tecnico_de_la_zona(bd, dueno, datos, contacto):
    _falla(dueno, datos)
    r = await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    assert (r.abiertos, r.asignados) == (1, 1)
    estado, prio, tecnico, nivel, _, vence, tid, _ = _ticket(dueno, datos)
    assert (estado, prio, tecnico, nivel) == ("asignado", "alta", datos["u_tecnico_1"], 0)
    assert vence == T0 + timedelta(hours=4)  # SLA de respuesta desde el inicio de la falla
    assert _eventos(dueno, tid) == ["apertura", "asignacion"]
    # Idempotente: otro ciclo no duplica el ticket ni los avisos.
    r2 = await ciclo(bd, T0 + timedelta(minutes=2), enviar=False)
    assert (r2.abiertos, r2.avisos) == (0, 0)
    assert dueno.execute("SELECT count(*) FROM idcmd.tickets").fetchone()[0] == 1


async def test_avisos_minimos_al_tecnico_y_al_cliente(bd, dueno, datos, contacto):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    filas = dueno.execute("SELECT destino, cuerpo FROM idcmd.notificaciones ORDER BY id").fetchall()
    por_destino = {d: c for d, c in filas}
    assert "seguridad@cliente-a.test" in por_destino and "tecnico_1@prueba.co" in por_destino
    cliente, tecnico = por_destino["seguridad@cliente-a.test"], por_destino["tecnico_1@prueba.co"]
    tid = _ticket(dueno, datos)[6]
    assert f"/seguimiento/{tid}/{firma_seguimiento(tid)}" in cliente
    assert f"/portal/tickets/{tid}" in tecnico
    for cuerpo in (cliente, tecnico):
        assert "10.0.0.10" not in cuerpo  # nunca la IP del equipo (principio 5)


async def test_sin_aceptar_a_tiempo_escala_a_coordinacion(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    await ciclo(bd, T0 + timedelta(minutes=20), enviar=False)  # aún dentro de los 30 min
    assert _ticket(dueno, datos)[3] == 0
    r = await ciclo(bd, T0 + timedelta(minutes=32), enviar=False)
    assert r.escalados == 1 and _ticket(dueno, datos)[3] == 2
    admin = dueno.execute(
        "SELECT count(*) FROM idcmd.notificaciones WHERE destino = 'admin@prueba.co'"
    ).fetchone()[0]
    assert admin == 1


async def test_sitio_sin_tecnico_avisa_a_coordinacion(bd, dueno, datos):
    _falla(dueno, datos, sitio="sitio_a3")
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    estado, _, tecnico, nivel, *_ = _ticket(dueno, datos, "sitio_a3")
    assert (estado, tecnico, nivel) == ("abierto", None, 2)


async def test_resuelto_se_cierra_solo_con_el_equipo_verificado(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        for accion in ("aceptar", "en_camino", "en_sitio", "resuelto"):
            await con.execute("SELECT idcmd.accion_ticket(%s, %s)", (tid, accion))
    # El técnico dice "resuelto" pero el equipo sigue caído: no se cierra.
    await ciclo(bd, T0 + timedelta(minutes=5), enviar=False)
    assert _ticket(dueno, datos)[0] == "resuelto"
    _recupera(dueno, datos, desde=T0 + timedelta(minutes=6))
    await ciclo(bd, T0 + timedelta(minutes=10), enviar=False)  # sano hace 4 min: espera
    assert _ticket(dueno, datos)[0] == "resuelto"
    await ciclo(bd, T0 + timedelta(minutes=17), enviar=False)  # sano hace 11 min
    estado, *_, cierre, _, _, _ = _ticket(dueno, datos)
    assert (estado, cierre) == ("cerrado", "verificado")


async def test_resuelto_pero_sigue_mal_se_reabre(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        await con.execute("SELECT idcmd.accion_ticket(%s, 'aceptar')", (tid,))
        await con.execute("SELECT idcmd.accion_ticket(%s, 'resuelto')", (tid,))
    resuelto_at = dueno.execute(
        "SELECT resuelto_at FROM idcmd.tickets WHERE id = %s", (tid,)
    ).fetchone()[0]
    await ciclo(bd, resuelto_at + timedelta(minutes=31), enviar=False)
    assert _ticket(dueno, datos)[0] == "aceptado"
    assert "reapertura" in _eventos(dueno, tid)


async def test_se_recupera_solo_antes_de_que_lo_atiendan(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    _recupera(dueno, datos, desde=T0 + timedelta(minutes=3))
    await ciclo(bd, T0 + timedelta(minutes=14), enviar=False)
    assert _ticket(dueno, datos)[4] == "recuperado_solo"


async def test_nueva_falla_mas_grave_sube_la_prioridad_sin_duplicar(bd, dueno, datos):
    _falla(dueno, datos, codigo="reloj_desfasado", estado="degradado")
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    assert _ticket(dueno, datos)[1] == "baja"
    _falla(dueno, datos, codigo="sin_respuesta_icmp")
    await ciclo(bd, T0 + timedelta(minutes=2), enviar=False)
    assert _ticket(dueno, datos)[1] == "alta"
    assert dueno.execute("SELECT count(*) FROM idcmd.tickets").fetchone()[0] == 1


async def test_sitio_sin_conexion(bd, dueno, datos):
    dueno.execute(
        "UPDATE idcmd.agentes SET ultimo_latido_at = %s WHERE id = %s",
        (T0 - timedelta(minutes=10), datos["agente_sitio_a2"]),
    )
    await ciclo(bd, T0, enviar=False)
    estado, prio, *_ = _ticket(dueno, datos, "sitio_a2")
    assert prio == "critica"
    dueno.execute(
        "UPDATE idcmd.agentes SET ultimo_latido_at = %s WHERE id = %s",
        (T0 + timedelta(minutes=5), datos["agente_sitio_a2"]),
    )
    await ciclo(bd, T0 + timedelta(minutes=5, seconds=30), enviar=False)
    assert _ticket(dueno, datos, "sitio_a2")[0] == "cerrado"


# --- Permisos sobre los tickets -------------------------------------------------------------------


async def test_solo_el_tecnico_asignado_avanza_el_ticket(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    for usuario in ("u_cliente_a", "u_tecnico_vencido"):
        with pytest.raises(psycopg.Error):
            async with bd.como_usuario(datos[usuario]) as con:
                await con.execute("SELECT idcmd.accion_ticket(%s, 'aceptar')", (tid,))
    with pytest.raises(psycopg.errors.CheckViolation):  # saltarse pasos no se puede
        async with bd.como_usuario(datos["u_tecnico_1"]) as con:
            await con.execute("SELECT idcmd.accion_ticket(%s, 'en_sitio')", (tid,))


async def test_cada_rol_ve_solo_sus_tickets(bd, dueno, datos):
    _falla(dueno, datos)
    _falla(dueno, datos, sitio="sitio_b1")
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    vistos = {}
    for usuario in ("u_admin", "u_tecnico_1", "u_cliente_b"):
        async with bd.como_usuario(datos[usuario]) as con:
            vistos[usuario] = (
                await (await con.execute("SELECT count(*) FROM idcmd.tickets")).fetchone()
            )[0]
    assert vistos == {"u_admin": 2, "u_tecnico_1": 1, "u_cliente_b": 1}


async def test_las_notas_internas_no_las_ve_el_cliente(bd, dueno, datos):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        await con.execute(
            "SELECT idcmd.accion_ticket(%s, 'nota', 'clave del NVR en la libreta')", (tid,)
        )
    async with bd.como_usuario(datos["u_cliente_a"]) as con:
        textos = [
            t
            for (t,) in await (
                await con.execute(
                    "SELECT texto FROM idcmd.eventos_ticket WHERE ticket_id = %s", (tid,)
                )
            ).fetchall()
        ]
    assert textos and not any("libreta" in t for t in textos)


async def test_envio_sin_canal_configurado_queda_registrado(bd, dueno, datos, contacto):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1))
    resultados = {
        r for (r,) in dueno.execute("SELECT resultado FROM idcmd.notificaciones").fetchall()
    }
    assert resultados == {"sin_canal"}


def test_firma_de_seguimiento_depende_de_la_llave(monkeypatch):
    import uuid

    tid = uuid.uuid4()
    a = firma_seguimiento(tid)
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", base64.b64encode(b"x" * 32).decode())
    assert firma_seguimiento(tid) != a and len(a) == 32


def test_avisos_no_llevan_detalles_tecnicos():
    import uuid

    asunto, cuerpo = avisos._mensaje(
        {
            "numero": 7, "sitio": "Sede norte", "equipo": "NVR", "titulo": "No responde",
            "texto": "Falla detectada", "ticket_id": uuid.uuid4(),
        },
        "cliente",
    )  # fmt: skip
    assert asunto == "[IDtech] Ticket #7 · Sede norte · NVR"
    assert "/seguimiento/" in cuerpo


# --- Pantallas ------------------------------------------------------------------------------------


@pytest.fixture
async def portal_tecnico(bd, dueno, datos, monkeypatch):
    """Portal con el técnico 1 ya ingresado (sesión creada directamente en la base)."""
    import httpx

    from idcmd_central.app import crear_app
    from idcmd_central.portal.seguridad import generar_token_sesion, hash_sesion

    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")

    async def cliente(usuario):
        token = generar_token_sesion()
        dueno.execute(
            "INSERT INTO idcmd.sesiones (token_hash, usuario_id, expira_at)"
            " VALUES (%s, %s, now() + interval '1 hour')",
            (hash_sesion(token), datos[usuario]),
        )
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=crear_app(bd)), base_url="http://portal.test"
        )
        c.cookies.set("idcmd_sesion", token)
        return c

    return cliente


async def test_tecnico_atiende_desde_el_portal(bd, dueno, datos, portal_tecnico):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    async with await portal_tecnico("u_tecnico_1") as c:
        lista = await c.get("/portal/tickets")
        assert lista.status_code == 200 and "Cámara sitio_a1" in lista.text
        pagina = await c.get(f"/portal/tickets/{tid}")
        assert "Aceptar" in pagina.text and "Falla detectada" in pagina.text
        r = await c.post(f"/portal/tickets/{tid}/accion", data={"accion": "aceptar"})
        assert r.status_code == 303 and "error" not in r.headers["location"]
        r = await c.post(f"/portal/tickets/{tid}/accion", data={"accion": "cancelar"})
        assert "error=" in r.headers["location"]  # solo un administrador cancela
    assert _ticket(dueno, datos)[0] == "aceptado"
    async with await portal_tecnico("u_cliente_a") as c:
        r = await c.post(f"/portal/tickets/{tid}/accion", data={"accion": "en_camino"})
        assert "error=" in r.headers["location"]
        assert (await c.get("/portal/avisos")).status_code == 404  # solo administradores


async def test_enlace_de_seguimiento_del_cliente(bd, dueno, datos, portal_tecnico):
    _falla(dueno, datos)
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = _ticket(dueno, datos)[6]
    async with await portal_tecnico("u_admin") as c:
        c.cookies.clear()  # el cliente no tiene sesión
        r = await c.get(f"/seguimiento/{tid}/{firma_seguimiento(tid)}")
        assert r.status_code == 200 and "Falla detectada" in r.text
        assert "10.0.0.10" not in r.text
        assert (await c.get(f"/seguimiento/{tid}/{'0' * 32}")).status_code == 404

"""App del técnico en campo: pasos de la base de conocimiento, llegada con ubicación, fotos,
firma, acta y la PWA."""

from __future__ import annotations

import filecmp
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from idcmd_central.app import crear_app
from idcmd_central.portal.seguridad import generar_llave, generar_token_sesion, hash_sesion
from idcmd_central.servicio import conocimiento
from idcmd_central.servicio.reglas import firma_seguimiento
from idcmd_central.servicio.worker import ciclo

T0 = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
RAIZ = Path(__file__).resolve().parents[2]


def _png() -> bytes:
    """PNG válido de 1x1 (la central valida el contenido, no la extensión)."""

    def bloque(tipo, datos):
        return (
            struct.pack(">I", len(datos))
            + tipo
            + datos
            + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
        )

    cabecera = bloque(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    datos = bloque(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + cabecera + datos + bloque(b"IEND", b"")


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_ARCHIVOS", str(tmp_path / "archivos"))
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")


@pytest.fixture
def ticket(dueno, datos):
    """Ticket del técnico 1 por la cámara de sitio_a1 (Hikvision), ya aceptado."""

    async def crear(bd):
        dueno.execute(
            "UPDATE idcmd.sitios SET lat = 10.9878, lon = -74.7889 WHERE id = %s",
            (datos["sitio_a1"],),
        )
        dueno.execute(
            "INSERT INTO idcmd.estado_actual (dispositivo_id, chequeo, cliente_id, sitio_id,"
            " estado, desde, codigo) VALUES (%s, 'icmp', %s, %s, 'caido', %s,"
            " 'sin_respuesta_icmp')",
            (datos["camara_sitio_a1"], datos["cliente_a"], datos["sitio_a1"], T0),
        )
        await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
        tid = dueno.execute("SELECT id FROM idcmd.tickets").fetchone()[0]
        async with bd.como_usuario(datos["u_tecnico_1"]) as con:
            await con.execute("SELECT idcmd.accion_ticket(%s, 'aceptar')", (tid,))
        return tid

    return crear


@pytest.fixture
def portal(bd, dueno, datos):
    def cliente(usuario):
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


def test_la_copia_de_la_base_de_conocimiento_es_identica_a_la_de_docs():
    assert filecmp.cmp(
        conocimiento.ARCHIVO, RAIZ / "docs/plantillas/base_conocimiento.csv", shallow=False
    )


def test_pasos_mas_especificos_primero():
    generico = conocimiento.pasos("sin_respuesta_icmp")
    camara = conocimiento.pasos("sin_respuesta_icmp", "camara", "dahua")
    assert generico and camara and generico != camara
    assert conocimiento.pasos("codigo_sin_fila") == conocimiento.GENERICOS


async def test_el_ticket_trae_los_pasos_y_no_se_resuelve_sin_completarlos(bd, dueno, datos, ticket):
    tid = await ticket(bd)
    pasos = dueno.execute(
        "SELECT id FROM idcmd.pasos_ticket WHERE ticket_id = %s ORDER BY orden", (tid,)
    ).fetchall()
    assert len(pasos) >= 2
    with pytest.raises(psycopg.errors.CheckViolation, match="complete la lista"):
        async with bd.como_usuario(datos["u_tecnico_1"]) as con:
            await con.execute("SELECT idcmd.accion_ticket(%s, 'resuelto')", (tid,))
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        for i, (paso,) in enumerate(pasos):
            await con.execute(
                "SELECT idcmd.marcar_paso(%s, %s)", (paso, "no_aplica" if i else "hecho")
            )
        await con.execute("SELECT idcmd.accion_ticket(%s, 'resuelto')", (tid,))
    # Otro técnico (o un cliente) no puede tocar la lista.
    with pytest.raises(psycopg.Error):
        async with bd.como_usuario(datos["u_cliente_a"]) as con:
            await con.execute("SELECT idcmd.marcar_paso(%s, 'pendiente')", (pasos[0][0],))


@pytest.mark.parametrize(
    ("lat", "lon", "texto"),
    [
        (10.9879, -74.7889, "ubicación confirmada a 11 m"),
        (11.0200, -74.7889, "fuera del radio"),
        (None, None, "no compartió su ubicación"),
    ],
)
async def test_llegada_con_ubicacion(bd, dueno, datos, ticket, lat, lon, texto):
    tid = await ticket(bd)
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        await con.execute("SELECT idcmd.llegada_ticket(%s, %s, %s, 12)", (tid, lat, lon))
    estado, ultimo = dueno.execute(
        "SELECT t.estado::text, (SELECT texto FROM idcmd.eventos_ticket WHERE ticket_id = t.id"
        " ORDER BY id DESC LIMIT 1) FROM idcmd.tickets t WHERE t.id = %s",
        (tid,),
    ).fetchone()
    assert estado == "en_sitio" and texto in ultimo


async def test_fotos_y_firma_desde_el_portal(bd, dueno, datos, ticket, portal):
    tid = await ticket(bd)
    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        await con.execute("SELECT idcmd.llegada_ticket(%s, 10.9879, -74.7889, 8)", (tid,))
    async with portal("u_tecnico_1") as c:
        r = await c.post(
            f"/portal/tickets/{tid}/fotos",
            files={"foto": ("x.png", _png(), "image/png")},
            data={"nota": "antes"},
        )
        assert "error" not in r.headers["location"]
        r = await c.post(
            f"/portal/tickets/{tid}/fotos", files={"foto": ("x.jpg", b"no soy foto", "image/jpeg")}
        )
        assert "error=" in r.headers["location"]  # se valida el contenido, no la extensión
        r = await c.post(
            f"/portal/tickets/{tid}/firma",
            files={"firma": ("f.png", _png(), "image/png")},
            data={"recibido_por": "María Pérez"},
        )
        assert "error" not in r.headers["location"]
        archivos = dueno.execute(
            "SELECT id, tipo FROM idcmd.archivos_ticket WHERE ticket_id = %s ORDER BY creado_at",
            (tid,),
        ).fetchall()
        assert [t for _, t in archivos] == ["foto", "firma"]
        foto = await c.get(f"/portal/archivos/{archivos[0][0]}")
        assert foto.status_code == 200 and foto.content == _png()
        pagina = await c.get(f"/portal/tickets/{tid}")
        assert "María Pérez" in pagina.text and "Lista de pasos" in pagina.text
        acta = await c.get(f"/portal/tickets/{tid}/acta")
        assert acta.status_code == 200 and "Acta de servicio" in acta.text
    # Un cliente de otra empresa no ve la foto, aunque tenga el enlace.
    async with portal("u_cliente_b") as c:
        assert (await c.get(f"/portal/archivos/{archivos[0][0]}")).status_code == 404
    # El cliente del sitio la ve en el acta de su enlace de seguimiento, sin sesión.
    async with portal("u_admin") as c:
        c.cookies.clear()
        base = f"/seguimiento/{tid}/{firma_seguimiento(tid)}"
        acta = await c.get(f"{base}/acta")
        assert acta.status_code == 200 and "María Pérez" in acta.text
        assert "(a 11 m)" in acta.text  # la llegada también llega al acta del cliente
        assert (await c.get(f"{base}/archivos/{archivos[0][0]}")).content == _png()
        otro = "00000000-0000-4000-8000-000000000000"
        assert (await c.get(f"{base}/archivos/{otro}")).status_code == 404


async def test_firma_sin_nombre_rechazada(bd, datos, ticket):
    tid = await ticket(bd)
    with pytest.raises(psycopg.errors.CheckViolation):
        async with bd.como_usuario(datos["u_tecnico_1"]) as con:
            await con.execute(
                "SELECT idcmd.registrar_archivo(%s, 'firma', 'image/png', 10, %s, NULL, '  ')",
                (tid, b"x" * 32),
            )


async def test_la_bateria_tambien_queda_en_el_ticket_del_tamper(bd, dueno, datos):
    """El panel informa un solo código (tamper); la batería viene en sus métricas."""
    from psycopg.types.json import Jsonb

    dueno.execute(
        "INSERT INTO idcmd.estado_actual (dispositivo_id, chequeo, cliente_id, sitio_id, estado,"
        " desde, codigo) VALUES (%s, 'hikvision_acceso', %s, %s, 'degradado', %s, 'tamper_activo')",
        (datos["camara_sitio_a1"], datos["cliente_a"], datos["sitio_a1"], T0),
    )
    dueno.execute(
        "INSERT INTO idcmd.metricas_actuales (dispositivo_id, chequeo, cliente_id, sitio_id,"
        " estado, metricas, observado_at) VALUES (%s, 'hikvision_acceso', %s, %s, 'degradado',"
        " %s, %s)",
        (
            datos["camara_sitio_a1"],
            datos["cliente_a"],
            datos["sitio_a1"],
            Jsonb({"tamper_activo": True, "bateria_baja": True}),
            T0,
        ),
    )
    await ciclo(bd, T0 + timedelta(minutes=1), enviar=False)
    tid = dueno.execute("SELECT id FROM idcmd.tickets").fetchone()[0]
    eventos = [
        t
        for (t,) in dueno.execute(
            "SELECT texto FROM idcmd.eventos_ticket WHERE ticket_id = %s", (tid,)
        ).fetchall()
    ]
    assert any("batería" in t.lower() for t in eventos)
    codigos = {
        c
        for (c,) in dueno.execute(
            "SELECT DISTINCT codigo FROM idcmd.pasos_ticket WHERE ticket_id = %s", (tid,)
        ).fetchall()
    }
    assert codigos == {"tamper_activo", "bateria_baja"}
    assert dueno.execute("SELECT count(*) FROM idcmd.tickets").fetchone()[0] == 1


async def test_pwa(bd):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=crear_app(bd)), base_url="http://portal.test"
    ) as c:
        manifiesto = await c.get("/portal/manifest.webmanifest")
        assert manifiesto.json()["scope"] == "/portal/"
        sw = await c.get("/portal/sw.js")
        assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
        assert (await c.get("/portal/estaticos/icono-512.png")).status_code == 200
        assert (await c.get("/portal/sin-conexion")).status_code == 200

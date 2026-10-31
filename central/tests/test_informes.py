"""Informe mensual de SLA de punta a punta: Postgres real, RLS, emisión, PDF y enlaces."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from idcmd_central.app import crear_app
from idcmd_central.informes import informe as inf
from idcmd_central.informes.emitir import emitir_mes
from idcmd_central.portal.informes import _html
from idcmd_central.portal.seguridad import generar_llave, generar_token_sesion, hash_sesion
from idcmd_central.servicio.reglas import enlace_informe, firma_informe

AHORA = datetime.now(UTC)
ACTUAL = inf.periodo_de(f"{AHORA.astimezone(inf.ZONA):%Y-%m}")
MES = inf.mes_anterior(ACTUAL)  # el mes de prueba ya cerró
DESDE, HASTA = inf.limites(MES)
H = timedelta(hours=1)
DIA = timedelta(days=1)


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_URL_PUBLICA", "https://command.idtech.test")
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")


def _evento(dueno, datos, momento, anterior, nuevo, sitio="sitio_a1"):
    dueno.execute(
        "INSERT INTO idcmd.eventos_estado (id, dispositivo_id, cliente_id, sitio_id, agente_id,"
        " chequeo, estado_anterior, estado_nuevo, observado_at) SELECT %s, id, cliente_id,"
        " sitio_id, %s, 'icmp', %s, %s, %s FROM idcmd.dispositivos WHERE id = %s",
        (uuid.uuid4(), datos[f"agente_{sitio}"], anterior, nuevo, momento,
         datos[f"camara_{sitio}"]),
    )  # fmt: skip


def _agregados(dueno, datos, desde, hasta, sin=(), sitio="sitio_a1"):
    """Un agregado por hora en [desde, hasta), salvo las horas de `sin` (agente sin reportar)."""
    dueno.execute(
        "INSERT INTO idcmd.agregados_hora (id, dispositivo_id, cliente_id, sitio_id, agente_id,"
        " chequeo, hora, muestras_ok, muestras_degradado, muestras_caido, muestras_desconocido)"
        " SELECT gen_random_uuid(), d.id, d.cliente_id, d.sitio_id, %s, 'icmp', h, 60, 0, 0, 0"
        " FROM idcmd.dispositivos d, generate_series(%s::timestamptz, %s::timestamptz - interval"
        " '1 hour', interval '1 hour') h WHERE d.id = %s AND NOT (h = ANY(%s))",
        (datos[f"agente_{sitio}"], desde, hasta, datos[f"camara_{sitio}"], list(sin)),
    )


@pytest.fixture
def mes_con_datos(dueno, datos):
    """sitio_a1 en el mes cerrado: 2 h caído, 5 h sin datos y 3 h de mantenimiento con caída."""
    # La semilla deja un evento "caído" ahora mismo; para este escenario empieza limpio.
    dueno.execute("DELETE FROM idcmd.eventos_estado")
    _evento(dueno, datos, DESDE - DIA, "desconocido", "ok")
    _evento(dueno, datos, DESDE + DIA + 10 * H, "ok", "caido")  # día 2, 10:00-12:00
    _evento(dueno, datos, DESDE + DIA + 12 * H, "caido", "ok")
    _evento(dueno, datos, DESDE + 9 * DIA + 8 * H, "ok", "caido")  # día 10, dentro del mant.
    _evento(dueno, datos, DESDE + 9 * DIA + 10 * H, "caido", "ok")
    sin = [DESDE + 4 * DIA + h * H for h in range(5)]  # día 5, 00:00-05:00: agente apagado
    _agregados(dueno, datos, DESDE - DIA, HASTA, sin=sin)
    dueno.execute(
        "INSERT INTO idcmd.exclusiones_sla (cliente_id, sitio_id, motivo, desde, hasta,"
        " descripcion) VALUES (%s, %s, 'mantenimiento', %s, %s, 'Cambio de UPS acordado')",
        (datos["cliente_a"], datos["sitio_a1"], DESDE + 9 * DIA + 7 * H, DESDE + 9 * DIA + 10 * H),
    )
    horas_mes = (HASTA - DESDE).total_seconds() / 3600
    medibles = horas_mes - 5 - 3  # sin datos y excluidas no entran
    return {"esperada": 100 * (medibles - 2) / medibles}


async def _como(bd, datos, usuario, fn):
    async with bd.como_usuario(datos[usuario]) as con:
        return await fn(con)


async def test_disponibilidad_del_mes_segun_adr_0004(bd, datos, mes_con_datos):
    r = await _como(bd, datos, "u_admin", lambda con: inf.construir(
        con, datos["sitio_a1"], MES, AHORA))  # fmt: skip
    assert r["disponibilidad"] == pytest.approx(mes_con_datos["esperada"])
    assert r["parcial"] is False
    assert r["totales"]["caido_s"] == 2 * 3600
    assert r["totales"]["sin_datos_s"] == 5 * 3600
    assert r["totales"]["excluido_s"] == 3 * 3600
    assert r["totales"]["caidas"] == 1  # la del mantenimiento quedó excluida
    assert r["exclusiones"][0]["descripcion"] == "Cambio de UPS acordado"
    assert r["cumple_meta"] is True  # 2 h caído en el mes: ~99,7 % >= meta 99,5 %
    assert len(r["tendencia"]) == inf.MESES_TENDENCIA


async def test_cada_rol_ve_solo_los_informes_de_sus_sitios(bd, datos, mes_con_datos):
    def construir(con):
        return inf.construir(con, datos["sitio_a1"], MES, AHORA)

    assert await _como(bd, datos, "u_cliente_a", construir) is not None
    assert await _como(bd, datos, "u_tecnico_1", construir) is not None
    assert await _como(bd, datos, "u_cliente_b", construir) is None
    assert await _como(bd, datos, "u_cliente_a3", construir) is None


async def test_emision_mensual_guarda_avisa_y_no_repite(bd, dueno, datos, mes_con_datos):
    dueno.execute(
        "INSERT INTO idcmd.contactos_sitio (cliente_id, sitio_id, nombre, correo)"
        " VALUES (%s, %s, 'Jefe de seguridad', 'seguridad@cliente.test')",
        (datos["cliente_a"], datos["sitio_a1"]),
    )
    dueno.execute("UPDATE idcmd.clientes SET informe_pdf_adjunto = true WHERE id = %s",
                  (datos["cliente_a"],))  # fmt: skip
    dia1 = inf.limites(ACTUAL)[0]
    assert await emitir_mes(bd, dia1 + 2 * H) == 0  # antes de las 6 a. m.: aún no
    assert await emitir_mes(bd, dia1 + 7 * H) == 4  # los 4 sitios del escenario
    assert await emitir_mes(bd, dia1 + 8 * H) == 0  # ya emitidos: no se repite
    disp, resumen = dueno.execute(
        "SELECT disponibilidad::float, resumen FROM idcmd.informes_mensuales"
        " WHERE sitio_id = %s AND periodo = %s",
        (datos["sitio_a1"], MES),
    ).fetchone()
    assert disp == pytest.approx(mes_con_datos["esperada"])
    avisos = dueno.execute(
        "SELECT destino, adjunto, cuerpo FROM idcmd.notificaciones WHERE sitio_id = %s",
        (datos["sitio_a1"],),
    ).fetchall()
    por_destino = {d: (a, c) for d, a, c in avisos}
    assert por_destino["seguridad@cliente.test"][0] == f"informe:{datos['sitio_a1']}:{MES:%Y-%m}"
    assert por_destino["admin@prueba.co"][0] is None  # coordinación recibe solo el enlace
    cuerpo = por_destino["seguridad@cliente.test"][1]
    assert enlace_informe(datos["sitio_a1"], f"{MES:%Y-%m}") in cuerpo
    assert "10.0.0" not in cuerpo  # sin IPs ni detalles técnicos
    # Sitios sin nada medido ni servicios: se emiten pero no se avisa a nadie.
    otros = dueno.execute(
        "SELECT count(*) FROM idcmd.notificaciones WHERE sitio_id = %s", (datos["sitio_b1"],)
    ).fetchone()[0]
    assert otros == 0


async def test_pdf_del_informe(bd, datos, mes_con_datos):
    r = await _como(bd, datos, "u_admin", lambda con: inf.construir(
        con, datos["sitio_a1"], MES, AHORA))  # fmt: skip
    from idcmd_central.informes.pdf import a_pdf

    html = _html(r)
    assert "Cambio de UPS acordado" in html and "<svg" in html
    pdf = a_pdf(html)
    assert pdf.startswith(b"%PDF-") and len(pdf) > 5000


@pytest.fixture
async def portal(bd, dueno, datos):
    async def cliente(usuario=None):
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


async def test_informe_en_el_portal(bd, datos, mes_con_datos, portal):
    ruta = f"/portal/informes/{datos['sitio_a1']}/{MES:%Y-%m}"
    async with await portal("u_cliente_a") as c:
        lista = await c.get("/portal/informes")
        assert lista.status_code == 200 and "sitio_a1" in lista.text
        pagina = await c.get(ruta)
        assert pagina.status_code == 200 and "Disponibilidad por equipo" in pagina.text
        assert "Enlace para el cliente" not in pagina.text  # solo el administrador lo comparte
        pdf = await c.get(ruta + "/pdf")
        assert pdf.headers["content-type"] == "application/pdf" and pdf.content[:5] == b"%PDF-"
        assert "informe-sitio-a1-" in pdf.headers["content-disposition"]
        mes_en_curso = await c.get(f"/portal/informes/{datos['sitio_a1']}/{ACTUAL:%Y-%m}")
        assert mes_en_curso.status_code == 200 and "Informe parcial" in mes_en_curso.text
        assert (await c.get(f"/portal/informes/{datos['sitio_a1']}/2099-01")).status_code == 404
        assert (await c.get(f"/portal/informes/{datos['sitio_a1']}/x")).status_code == 404
    async with await portal("u_cliente_b") as c:
        assert (await c.get(ruta)).status_code == 404
    async with await portal() as c:
        assert (await c.get(ruta)).status_code == 303  # sin ingreso


async def test_enlace_del_cliente_solo_con_informe_emitido(bd, datos, mes_con_datos, portal):
    sitio, periodo = datos["sitio_a1"], f"{MES:%Y-%m}"
    ruta = f"/informe/{sitio}/{periodo}/{firma_informe(sitio, periodo)}"
    async with await portal() as c:
        assert (await c.get(ruta)).status_code == 404  # aún no emitido: no hay cálculo en vivo
        await emitir_mes(bd, inf.limites(ACTUAL)[0] + 7 * H)
        r = await c.get(ruta)
        assert r.status_code == 200 and "Informe de servicio" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert (await c.get(ruta + "/pdf")).content[:5] == b"%PDF-"
        otro = f"/informe/{datos['sitio_a2']}/{periodo}/{firma_informe(sitio, periodo)}"
        assert (await c.get(otro)).status_code == 404  # la firma es de otro sitio
        alterada = ruta[:-1] + ("1" if ruta.endswith("0") else "0")  # siempre otra firma
        assert (await c.get(alterada)).status_code == 404


def test_consola_exclusiones_meta_y_emision(base_pruebas, dueno, datos, mes_con_datos, monkeypatch,
                                            tmp_path, capsys):  # fmt: skip
    from idcmd_central.cli import main

    monkeypatch.setenv("IDCMD_DSN_ADMIN", base_pruebas["dueno"])
    dia = inf.limites(MES)[0].astimezone(inf.ZONA) + 14 * DIA
    desde, hasta = f"{dia:%Y-%m-%d} 08:00", f"{dia:%Y-%m-%d} 09:30"
    crear = ["exclusiones", "crear", "--sitio", "SITIO_A1", "--equipo", "Cámara sitio_a1",
             "--motivo", "causa_cliente", "--desde", desde, "--hasta", hasta,
             "--descripcion", "Corte de energía del edificio"]  # fmt: skip
    assert main(crear) == 0
    assert main(["clientes", "sla", "--cliente", "Integradora A", "--meta", "99.9"]) == 0
    assert main(["informe", "emitir", "--mes", f"{MES:%Y-%m}", "--sitio", "sitio_a1"]) == 0
    meta, excluido = dueno.execute(
        "SELECT (resumen->>'meta')::float, (resumen->'totales'->>'excluido_s')::float"
        " FROM idcmd.informes_mensuales WHERE sitio_id = %s",
        (datos["sitio_a1"],),
    ).fetchone()
    assert meta == 99.9 and excluido == (3 + 1.5) * 3600
    salida = tmp_path / "informe.pdf"
    assert main(["informe", "pdf", "--sitio", "sitio_a1", "--mes", f"{MES:%Y-%m}",
                 "--salida", str(salida)]) == 0  # fmt: skip
    assert salida.read_bytes()[:5] == b"%PDF-"
    with pytest.raises(SystemExit):
        main(["informe", "emitir", "--mes", f"{ACTUAL:%Y-%m}"])  # el mes en curso no se emite

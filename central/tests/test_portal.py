"""Portal: ingreso con clave + TOTP, bloqueo, sesiones, visibilidad por rol (RLS) y auditoría."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

from idcmd_central import laboratorio
from idcmd_central.app import crear_app
from idcmd_central.portal.seguridad import (
    cifrar_secreto,
    codigo_totp,
    generar_llave,
    generar_secreto,
    verificar_totp,
)

CLAVE = "Clave-Portal-2026"
LLAVE = generar_llave()


# --- TOTP (RFC 6238, apéndice B, SHA-1, recortado a 6 dígitos) ----------------------------

SECRETO_RFC = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("momento", "codigo"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_totp_vectores_rfc_6238(momento, codigo):
    assert codigo_totp(SECRETO_RFC, momento) == codigo


def test_totp_tolera_un_paso_y_no_mas():
    t = 1_800_000_000
    assert verificar_totp(SECRETO_RFC, codigo_totp(SECRETO_RFC, t - 30), t)
    assert not verificar_totp(SECRETO_RFC, codigo_totp(SECRETO_RFC, t - 90), t)
    assert not verificar_totp(SECRETO_RFC, "12a456", t)


# --- Escenario --------------------------------------------------------------------------


@pytest.fixture
def credenciales(datos, dueno, monkeypatch):
    """Clave y segundo factor para el admin y el técnico 1 del escenario."""
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", LLAVE)
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")
    secretos = {}
    for usuario in ("admin", "tecnico_1"):
        secreto = generar_secreto()
        secretos[usuario] = secreto
        dueno.execute(
            "UPDATE idcmd.usuarios SET hash_clave = crypt(%s, gen_salt('bf', 4)),"
            " totp_cifrado = %s WHERE id = %s",
            (CLAVE, cifrar_secreto(secreto, base64.b64decode(LLAVE)), datos[f"u_{usuario}"]),
        )
    return secretos


@pytest.fixture
async def portal(bd):
    app = crear_app(bd)
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://portal.test") as c:
        yield c


async def _entrar(portal, usuario, secreto, clave=CLAVE, codigo=None):
    return await portal.post(
        "/portal/entrar",
        data={
            "correo": f"{usuario}@prueba.co",
            "clave": clave,
            "codigo": codigo or codigo_totp(secreto),
        },
    )


# --- Ingreso ------------------------------------------------------------------------------


async def test_ingreso_correcto_y_estado_por_sitio(portal, credenciales, datos):
    r = await _entrar(portal, "admin", credenciales["admin"])
    assert r.status_code == 303 and r.headers["location"] == "/portal"
    assert "idcmd_sesion" in r.cookies
    inicio = await portal.get("/portal")
    assert inicio.status_code == 200
    for sitio in ("sitio_a1", "sitio_a2", "sitio_a3", "sitio_b1"):
        assert sitio in inicio.text
    detalle = await portal.get(f"/portal/sitios/{datos['sitio_a1']}")
    assert detalle.status_code == 200 and "Cámara sitio_a1" in detalle.text


async def test_clave_equivocada_o_codigo_equivocado(portal, credenciales, dueno, datos):
    r = await _entrar(portal, "admin", credenciales["admin"], clave="otra-clave-cualquiera")
    assert r.status_code == 401 and "idcmd_sesion" not in r.cookies
    r = await _entrar(portal, "admin", credenciales["admin"], codigo="000000")
    assert r.status_code == 401
    intentos = dueno.execute(
        "SELECT intentos_fallidos FROM idcmd.usuarios WHERE id = %s", (datos["u_admin"],)
    ).fetchone()[0]
    assert intentos == 2  # el código TOTP malo también cuenta
    fallidos = dueno.execute(
        "SELECT count(*) FROM idcmd.auditoria_accesos WHERE accion = 'ingreso_fallido'"
    ).fetchone()[0]
    assert fallidos == 2


async def test_cinco_fallos_bloquean_aunque_luego_acierte(portal, credenciales):
    for _ in range(5):
        await _entrar(portal, "admin", credenciales["admin"], clave="mala-mala-mala")
    r = await _entrar(portal, "admin", credenciales["admin"])
    assert r.status_code == 401


async def test_con_la_clave_robada_no_se_pueden_probar_codigos_sin_fin(portal, credenciales):
    for _ in range(5):
        await _entrar(portal, "admin", credenciales["admin"], codigo="000000")
    r = await _entrar(portal, "admin", credenciales["admin"])  # código correcto: ya bloqueado
    assert r.status_code == 401


async def test_ingreso_correcto_reinicia_el_contador(portal, credenciales, dueno, datos):
    for _ in range(3):
        await _entrar(portal, "admin", credenciales["admin"], codigo="000000")
    assert (await _entrar(portal, "admin", credenciales["admin"])).status_code == 303
    intentos = dueno.execute(
        "SELECT intentos_fallidos FROM idcmd.usuarios WHERE id = %s", (datos["u_admin"],)
    ).fetchone()[0]
    assert intentos == 0


async def test_correo_inexistente_responde_igual(portal, credenciales):
    r = await _entrar(portal, "nadie", credenciales["admin"])
    assert r.status_code == 401


async def test_sin_sesion_redirige_y_el_refresco_pide_ingreso(portal, credenciales, datos):
    r = await portal.get("/portal")
    assert r.status_code == 303 and r.headers["location"] == "/portal/entrar"
    r = await portal.get(f"/portal/sitios/{datos['sitio_a1']}/equipos")
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/portal/entrar"


async def test_salir_invalida_la_sesion(portal, credenciales):
    await _entrar(portal, "admin", credenciales["admin"])
    token = portal.cookies["idcmd_sesion"]
    await portal.post("/portal/salir")
    portal.cookies.set("idcmd_sesion", token)  # alguien que guardó la cookie
    r = await portal.get("/portal")
    assert r.status_code == 303


async def test_formulario_desde_otro_origen_rechazado(portal, credenciales):
    r = await portal.post(
        "/portal/entrar",
        data={"correo": "admin@prueba.co", "clave": CLAVE, "codigo": "123456"},
        headers={"Origin": "https://sitio-malicioso.example"},
    )
    assert r.status_code == 403


async def test_cabeceras_de_seguridad(portal):
    r = await portal.get("/portal/entrar")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


# --- Visibilidad por rol (la RLS decide) ---------------------------------------------------


async def test_tecnico_ve_solo_su_sitio(portal, credenciales, datos):
    await _entrar(portal, "tecnico_1", credenciales["tecnico_1"])
    inicio = await portal.get("/portal")
    assert "sitio_a1" in inicio.text
    assert "sitio_a2" not in inicio.text and "sitio_b1" not in inicio.text
    ajeno = await portal.get(f"/portal/sitios/{datos['sitio_b1']}")
    assert ajeno.status_code == 404  # igual que si no existiera
    ajeno = await portal.get(f"/portal/sitios/{datos['sitio_b1']}/auditoria")
    assert ajeno.status_code == 404


async def test_auditoria_del_sitio(portal, credenciales, datos, dueno):
    dueno.execute(
        "INSERT INTO idcmd.operaciones_equipo (id, dispositivo_id, cliente_id, sitio_id,"
        " agente_id, ocurrido_at, tipo, cuenta, ip_origen, codigo_fabricante)"
        " VALUES (%s, %s, %s, %s, %s, now(), 'cambio_configuracion', 'admin', '10.0.0.9',"
        " 'remoteCfgParm')",
        (
            uuid.uuid4(),
            datos["camara_sitio_a1"],
            datos["cliente_a"],
            datos["sitio_a1"],
            datos["agente_sitio_a1"],
        ),
    )
    await _entrar(portal, "admin", credenciales["admin"])
    r = await portal.get(f"/portal/sitios/{datos['sitio_a1']}/auditoria")
    assert r.status_code == 200
    assert "Cambio de configuración" in r.text and "10.0.0.9" in r.text
    vistas = dueno.execute(
        "SELECT count(*) FROM idcmd.auditoria_accesos WHERE usuario_id = %s AND accion = 'ver'",
        (datos["u_admin"],),
    ).fetchone()[0]
    assert vistas == 1


# --- El portal nunca lee secretos ----------------------------------------------------------


@pytest.mark.parametrize(
    "consulta",
    [
        "SELECT hash_clave FROM idcmd.usuarios",
        "SELECT totp_cifrado FROM idcmd.usuarios",
        "SELECT intentos_fallidos FROM idcmd.usuarios",
        "SELECT * FROM idcmd.sesiones",
    ],
)
async def test_rol_del_portal_no_lee_hashes_ni_sesiones(bd, credenciales, datos, consulta):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with bd.como_usuario(datos["u_admin"]) as con:
            await con.execute(consulta)


# --- Inventario de laboratorio ------------------------------------------------------------


def test_inventario_laboratorio_genera_config_valida_del_agente(base_pruebas, datos, tmp_path):
    from idcmd_agente.config import ConfigAgente

    archivo: Path = tmp_path / "oficina.yaml"
    archivo.write_text(
        """
cliente: IDtech
regional: Barranquilla
sitio: Oficina
dispositivos:
  - clave: nvr
    nombre: NVR oficina
    tipo_equipo: nvr
    marca: dahua
    direccion: 10.0.0.16
    credencial_ref: nvr
    canales: {1: camara_1}
    chequeos:
      - {adaptador: dahua_nvr, con_canales: true}
      - {adaptador: dahua_registro, intervalo_s: 300, deteccion: false}
  - clave: camara_1
    nombre: Cámara entrada
    tipo_equipo: camara
    direccion: 10.0.0.21
    chequeos:
      - {adaptador: icmp}
""",
        encoding="utf-8",
    )
    datos_inv = laboratorio.leer(archivo)
    primero = laboratorio.cargar(base_pruebas["dueno"], datos_inv)
    segundo = laboratorio.cargar(base_pruebas["dueno"], datos_inv)
    assert primero == segundo  # idempotente: mismos ids, sin duplicados
    config = laboratorio.config_agente(datos_inv, primero, uuid.uuid4(), "http://localhost:8000")
    validada = ConfigAgente.model_validate(config)
    nvr = next(o for o in validada.objetivos if o.adaptador == "dahua_nvr")
    assert nvr.canales == {1: primero.dispositivos["camara_1"]}
    assert nvr.credencial_ref == "nvr"
    ping = next(o for o in validada.objetivos if o.adaptador == "icmp")
    assert ping.credencial_ref is None

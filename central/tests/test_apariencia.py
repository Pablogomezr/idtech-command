"""Apariencia de los equipos: la figura sale de la referencia; la foto por modelo la sube solo
el administrador, llega reducida y sin metadatos, y la ve quien entra al portal."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from idcmd_central.app import crear_app
from idcmd_central.portal.apariencia import clave_modelo, forma
from idcmd_central.portal.modelos import FotoInvalida, preparar_foto
from idcmd_central.portal.seguridad import generar_llave, generar_token_sesion, hash_sesion

ORIGEN = {"origin": "http://portal.test"}


@pytest.mark.parametrize(
    ("tipo", "modelo", "esperada"),
    [
        ("camara", "DS-2CD2T47G2-L", "bala"),
        ("camara", "DS-2CD2047G2-LU", "bala"),
        ("camara", "DS-2CD2147G2-SU", "domo"),
        ("camara", "DS-2CD2347G2-LU", "torreta"),
        ("camara", "DS-2DE4425IW-DE", "ptz"),
        ("camara", "DS-2CD6365G0E-IVS", "ojo_pez"),
        ("camara", "IPC-HFW2441S-S", "bala"),
        ("camara", "DH-IPC-HDBW2441E-S", "domo"),
        ("camara", "IPC-HDW2441T-S", "torreta"),
        ("camara", "SD49425XB-HNR", "ptz"),
        ("camara", "Cámara genérica", "bala"),  # no reconocida: la de su tipo
        ("camara", None, "bala"),
        ("nvr", "DHI-NVR5432-EI", "grabador"),
        ("controladora", "DS-K2602T", "panel"),
        ("controladora", "DS-K1T341AMF", "terminal"),
        ("lectora", "DS-K1102AM", "lectora"),
        ("controladora", "ASI7213X", "terminal"),
        ("ups", None, "ups"),
        (None, None, "otro"),
    ],
)
def test_forma_segun_la_referencia(tipo, modelo, esperada):
    assert forma(tipo, modelo) == esperada


def test_clave_del_modelo_normalizada():
    assert clave_modelo(" ds-2cd2t47g2-l ") == clave_modelo("DS-2CD2T47G2-L") == "DS-2CD2T47G2-L"
    assert clave_modelo("  ") is None and clave_modelo(None) is None


def _foto(formato="JPEG", tam=(2000, 1500), con_exif=True) -> bytes:
    imagen = Image.new("RGB", tam, (200, 30, 30))
    salida = io.BytesIO()
    extra = {}
    if con_exif:
        exif = Image.Exif()
        exif[0x013B] = "Persona que tomó la foto"  # Artist
        extra["exif"] = exif
    imagen.save(salida, formato, **extra)
    return salida.getvalue()


def test_la_foto_se_reduce_y_pierde_los_metadatos():
    mime, datos = preparar_foto(_foto())
    imagen = Image.open(io.BytesIO(datos))
    assert mime == "image/webp" and imagen.format == "WEBP"
    assert max(imagen.size) == 480 and len(datos) < 60_000
    assert not imagen.getexif() and b"Persona" not in datos


@pytest.mark.parametrize(
    "contenido", [b"no es una imagen", b"GIF89a" + b"\0" * 50, _foto("GIF", con_exif=False)]
)
def test_rechaza_lo_que_no_es_foto(contenido):
    with pytest.raises(FotoInvalida):
        preparar_foto(contenido)


@pytest.fixture
def portal(bd, dueno, datos, monkeypatch):
    monkeypatch.setenv("IDCMD_LLAVE_TOTP", generar_llave())
    monkeypatch.setenv("IDCMD_COOKIE_SEGURA", "false")

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


async def test_foto_por_modelo_de_punta_a_punta(portal, dueno, datos):
    dueno.execute("UPDATE idcmd.dispositivos SET modelo = 'DS-2CD2147G2-SU' WHERE id = %s",
                  (datos["camara_sitio_a1"],))  # fmt: skip
    sitio = f"/portal/sitios/{datos['sitio_a1']}"
    subida = {"archivo": ("camara.jpg", _foto(), "image/jpeg")}
    async with portal("u_cliente_a") as c:
        r = await c.get(sitio)
        assert 'class="figura"' in r.text and "Cámara domo" in r.text  # ilustración del domo
        assert "/portal/modelos/foto/" not in r.text
    for quien in ("u_tecnico_1", "u_cliente_a"):  # solo el administrador cambia la apariencia
        async with portal(quien) as c:
            r = await c.post("/portal/admin/apariencia/foto", headers=ORIGEN,
                             data={"modelo": "DS-2CD2147G2-SU"}, files=subida)  # fmt: skip
            assert r.status_code == 404
    async with portal("u_admin") as c:
        lista = await c.get("/portal/admin/apariencia")
        assert "DS-2CD2147G2-SU" in lista.text and "Subir foto" in lista.text
        r = await c.post("/portal/admin/apariencia/foto", headers=ORIGEN,
                         data={"modelo": "ds-2cd2147g2-su"}, files=subida)  # fmt: skip
        assert r.status_code == 303 and "foto%20actualizada" in r.headers["location"]
        malo = await c.post("/portal/admin/apariencia/foto", headers=ORIGEN,
                            data={"modelo": "X"},
                            files={"archivo": ("a.jpg", b"texto", "image/jpeg")})  # fmt: skip
        assert "no%20es%20una%20foto" in malo.headers["location"]
    async with portal("u_cliente_a") as c:
        r = await c.get(sitio)
        url = r.text.split('src="/portal/modelos/foto/')[1].split('"')[0]
        foto = await c.get("/portal/modelos/foto/" + url)
        assert foto.status_code == 200 and foto.headers["content-type"] == "image/webp"
        assert "immutable" in foto.headers["cache-control"]
    async with portal() as c:  # sin sesión no se entrega
        assert (await c.get("/portal/modelos/foto/" + url)).status_code == 404
    async with portal("u_admin") as c:
        r = await c.post("/portal/admin/apariencia/quitar", headers=ORIGEN,
                         data={"modelo": "DS-2CD2147G2-SU"})  # fmt: skip
        assert r.status_code == 303
    assert dueno.execute("SELECT count(*) FROM idcmd.fotos_modelo").fetchone()[0] == 0
    acciones = dueno.execute(
        "SELECT count(*) FROM idcmd.auditoria_accesos WHERE accion = 'admin_foto_modelo'"
    ).fetchone()[0]
    assert acciones == 2


async def test_la_base_no_deja_escribir_fotos_a_quien_no_es_admin(bd, datos):
    import psycopg

    async with bd.como_usuario(datos["u_tecnico_1"]) as con:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await con.execute(
                "INSERT INTO idcmd.fotos_modelo (modelo, mime, contenido, sha256)"
                " VALUES ('X', 'image/webp', '\\x00', '\\x00')"
            )

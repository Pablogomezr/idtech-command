import gzip
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from idcmd_central.app import crear_app

T0 = datetime(2026, 11, 2, 13, 0, tzinfo=UTC)


@pytest.fixture
async def cliente(bd):
    transporte = httpx.ASGITransport(app=crear_app(bd))
    async with httpx.AsyncClient(transport=transporte, base_url="http://central") as c:
        yield c


def _lote(agente_id, eventos=(), agregados=()):
    return {
        "version_contrato": "1",
        "agente_id": str(agente_id),
        "enviado_at": T0.isoformat(),
        "latido": {
            "agente_version": "0.1.0",
            "reloj_agente": T0.isoformat(),
            "inicio_sesion": (T0 - timedelta(hours=1)).isoformat(),
            "pendientes": 0,
            "adaptadores": ["icmp"],
        },
        "eventos": list(eventos),
        "agregados": list(agregados),
    }


def _evento(dispositivo_id, nuevo="caido", minutos=0, **extra):
    return {
        "id": str(uuid.uuid4()),
        "dispositivo_id": str(dispositivo_id),
        "chequeo": "icmp",
        "estado_anterior": "ok",
        "estado_nuevo": nuevo,
        "observado_at": (T0 + timedelta(minutes=minutos)).isoformat(),
        "codigo": "sin_respuesta_icmp" if nuevo == "caido" else None,
        "metricas": {},
        **extra,
    }


async def _enviar(cliente, token, cuerpo, comprimir=True, cabeceras=None):
    datos = json.dumps(cuerpo).encode()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if comprimir:
        datos = gzip.compress(datos)
        h["Content-Encoding"] = "gzip"
    h.update(cabeceras or {})
    return await cliente.post("/api/v1/ingesta", content=datos, headers=h)


async def test_lote_valido_registra_eventos_estado_y_latido(cliente, datos, dueno):
    camara = datos["camara_sitio_a1"]
    lote = _lote(datos["agente_sitio_a1"], [_evento(camara)])
    r = await _enviar(cliente, datos.tokens["sitio_a1"], lote)
    assert r.status_code == 200
    assert r.json() == {"aceptados": 1, "rechazados": 0, "cambios": 0, "comandos": []}

    estado, desde = dueno.execute(
        "SELECT estado, desde FROM idcmd.estado_actual WHERE dispositivo_id = %s", (camara,)
    ).fetchone()
    assert (estado, desde) == ("caido", T0)
    version, latido = dueno.execute(
        "SELECT version, ultimo_latido_at FROM idcmd.agentes WHERE id = %s",
        (datos["agente_sitio_a1"],),
    ).fetchone()
    assert version == "0.1.0" and latido is not None


async def test_reenviar_el_mismo_lote_no_duplica(cliente, datos, dueno):
    lote = _lote(datos["agente_sitio_a1"], [_evento(datos["camara_sitio_a1"])])
    for _ in range(3):
        assert (await _enviar(cliente, datos.tokens["sitio_a1"], lote)).status_code == 200
    (n,) = dueno.execute(
        "SELECT count(*) FROM idcmd.eventos_estado WHERE id = %s", (lote["eventos"][0]["id"],)
    ).fetchone()
    assert n == 1


async def test_solo_latido_sin_items(cliente, datos):
    r = await _enviar(cliente, datos.tokens["sitio_a1"], _lote(datos["agente_sitio_a1"]))
    assert r.status_code == 200 and r.json()["aceptados"] == 0


async def test_evento_atrasado_no_pisa_el_estado_actual(cliente, datos, dueno):
    camara = datos["camara_sitio_a1"]
    token, agente = datos.tokens["sitio_a1"], datos["agente_sitio_a1"]
    await _enviar(cliente, token, _lote(agente, [_evento(camara, "ok", minutos=30)]))
    await _enviar(cliente, token, _lote(agente, [_evento(camara, "caido", minutos=10)]))
    (estado,) = dueno.execute(
        "SELECT estado FROM idcmd.estado_actual WHERE dispositivo_id = %s", (camara,)
    ).fetchone()
    assert estado == "ok"
    # Pero el evento atrasado sí queda en el historial (cuenta para la disponibilidad).
    (n,) = dueno.execute(
        "SELECT count(*) FROM idcmd.eventos_estado WHERE dispositivo_id = %s AND observado_at = %s",
        (camara, T0 + timedelta(minutes=10)),
    ).fetchone()
    assert n == 1


async def test_dispositivo_de_otro_sitio_se_rechaza(cliente, datos, dueno):
    ajeno = _evento(datos["camara_sitio_b1"])
    lote = _lote(datos["agente_sitio_a1"], [ajeno, _evento(datos["camara_sitio_a1"])])
    r = await _enviar(cliente, datos.tokens["sitio_a1"], lote)
    assert r.status_code == 200
    assert r.json()["rechazados"] == 1 and r.json()["aceptados"] == 1
    assert (
        dueno.execute("SELECT 1 FROM idcmd.eventos_estado WHERE id = %s", (ajeno["id"],)).fetchone()
        is None
    )


async def test_agregados_horarios(cliente, datos, dueno):
    agregado = {
        "id": str(uuid.uuid4()),
        "dispositivo_id": str(datos["camara_sitio_a1"]),
        "chequeo": "icmp",
        "hora": T0.isoformat(),
        "muestras_ok": 58,
        "muestras_degradado": 0,
        "muestras_caido": 2,
        "muestras_desconocido": 0,
        "latencia_prom_ms": 3.2,
        "latencia_max_ms": 9.1,
    }
    lote = _lote(datos["agente_sitio_a1"], agregados=[agregado])
    for _ in range(2):
        r = await _enviar(cliente, datos.tokens["sitio_a1"], lote)
    assert r.status_code == 200
    (n,) = dueno.execute("SELECT count(*) FROM idcmd.agregados_hora").fetchone()
    assert n == 1


@pytest.mark.parametrize("token", ["", "idcmd_token-inventado", None])
async def test_token_invalido(cliente, datos, token):
    cabeceras = {} if token is None else {"Authorization": f"Bearer {token}"}
    r = await cliente.post("/api/v1/ingesta", content=b"{}", headers=cabeceras)
    assert r.status_code == 401


async def test_token_revocado(cliente, datos, dueno):
    dueno.execute(
        "UPDATE idcmd.agentes SET revocado_at = now() WHERE id = %s", (datos["agente_sitio_a1"],)
    )
    r = await _enviar(cliente, datos.tokens["sitio_a1"], _lote(datos["agente_sitio_a1"]))
    assert r.status_code == 401


async def test_lote_con_agente_id_ajeno(cliente, datos):
    r = await _enviar(cliente, datos.tokens["sitio_a1"], _lote(datos["agente_sitio_b1"]))
    assert r.status_code == 409


@pytest.mark.parametrize(
    "dañar",
    [
        lambda e: e.update(metricas={"clave": "admin123"}),  # fuera del vocabulario común
        lambda e: e.update(usuario="admin"),  # campo no declarado
        lambda e: e.update(observado_at="2026-11-02T13:00:00"),  # sin zona horaria
        lambda e: e.update(estado_nuevo="explotado"),
    ],
)
async def test_lote_invalido(cliente, datos, dañar):
    evento = _evento(datos["camara_sitio_a1"])
    dañar(evento)
    r = await _enviar(cliente, datos.tokens["sitio_a1"], _lote(datos["agente_sitio_a1"], [evento]))
    assert r.status_code == 422
    assert "admin123" not in r.text


async def test_bomba_gzip(cliente, datos):
    bomba = gzip.compress(b"0" * (60 * 1024 * 1024))
    r = await cliente.post(
        "/api/v1/ingesta",
        content=bomba,
        headers={"Authorization": f"Bearer {datos.tokens['sitio_a1']}", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 413


async def test_codificacion_no_soportada(cliente, datos):
    r = await _enviar(
        cliente,
        datos.tokens["sitio_a1"],
        _lote(datos["agente_sitio_a1"]),
        comprimir=False,
        cabeceras={"Content-Encoding": "br"},
    )
    assert r.status_code == 415


async def test_salud(cliente):
    r = await cliente.get("/salud")
    assert r.status_code == 200 and r.json()["estado"] == "ok"

"""El contrato de la central coincide con el del agente, y el agente REAL entrega datos a la
central REAL sobre Postgres real (sin simulaciones intermedias)."""

import json
import uuid
from pathlib import Path

import httpx
from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.enviador import ConfigEnvio, Enviador
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Latido, ResultadoChequeo, ahora_utc

from idcmd_central.app import crear_app
from idcmd_central.contrato import LoteIngesta

ESQUEMA = Path(__file__).resolve().parents[2] / "contrato" / "ingesta-v1.schema.json"


def _sin_descripciones(esquema):
    if isinstance(esquema, dict):
        return {k: _sin_descripciones(v) for k, v in esquema.items() if k != "description"}
    if isinstance(esquema, list):
        return [_sin_descripciones(v) for v in esquema]
    return esquema


def test_contrato_de_la_central_coincide_con_el_esquema_compartido():
    esperado = _sin_descripciones(json.loads(ESQUEMA.read_text()))
    assert _sin_descripciones(LoteIngesta.model_json_schema()) == esperado


async def test_agente_real_entrega_a_la_central_real(bd, datos, dueno, tmp_path):
    camara = datos["camara_sitio_a2"]
    buzon = Buzon(tmp_path / "buzon.sqlite3")
    maquina = MaquinaEstados()
    agregador = Agregador(sesion=uuid.uuid4())

    # La cámara responde y luego deja de responder tres veces: caída confirmada.
    for estado in [Estado.OK, Estado.CAIDO, Estado.CAIDO, Estado.CAIDO]:
        r = ResultadoChequeo(
            dispositivo_id=camara,
            chequeo="icmp",
            estado=estado,
            metricas={"latencia_ms": 2.5} if estado == Estado.OK else {},
        )
        agregador.registrar(r)
        if evento := maquina.procesar(r):
            buzon.registrar_transicion(evento, ahora_utc())
    for agregado in agregador.vaciar():
        buzon.encolar_agregado(agregado, ahora_utc())

    def latido():
        return Latido(
            agente_version="0.1.0",
            reloj_agente=ahora_utc(),
            inicio_sesion=ahora_utc(),
            pendientes=buzon.pendientes(),
            adaptadores=["icmp"],
        )

    cliente = httpx.AsyncClient(transport=httpx.ASGITransport(app=crear_app(bd)))
    enviador = Enviador(
        ConfigEnvio(
            url_central="http://localhost",
            token=datos.tokens["sitio_a2"],
            agente_id=datos["agente_sitio_a2"],
        ),
        buzon,
        latido,
        cliente=cliente,
    )
    assert await enviador.enviar_pendientes() == 3  # 2 eventos + 1 agregado
    assert buzon.pendientes() == 0

    # Lo que ve un usuario Cliente del tenant A en el portal:
    async with bd.como_usuario(datos["u_cliente_a"]) as con:
        cur = await con.execute(
            "SELECT estado, codigo FROM idcmd.estado_actual WHERE dispositivo_id = %s", (camara,)
        )
        assert await cur.fetchone() == ("caido", None)
    # ...y lo que NO ve el del tenant B:
    async with bd.como_usuario(datos["u_cliente_b"]) as con:
        cur = await con.execute("SELECT count(*) FROM idcmd.estado_actual")
        assert (await cur.fetchone())[0] == 0
    await enviador.cerrar()


def test_vocabulario_de_metricas_igual_en_agente_y_central():
    from idcmd_agente.nucleo.modelos import METRICAS_PERMITIDAS as DEL_AGENTE

    from idcmd_central.contrato import METRICAS_PERMITIDAS as DE_LA_CENTRAL

    assert DEL_AGENTE == DE_LA_CENTRAL

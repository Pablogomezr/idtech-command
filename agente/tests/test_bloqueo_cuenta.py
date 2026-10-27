"""La cuenta del equipo no debe quedar bloqueada por culpa del agente.

Caso real (oficina, 2026-10-27): tras varios intentos fallidos el NVR Dahua respondió 403
con RmLock (segundos de bloqueo restantes) y RmLogin 0 a todas las consultas.
"""

import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import respx
from conftest import T0, RelojFalso
from pydantic import SecretStr

from idcmd_agente.adaptadores.dahua_nvr import AdaptadorDahuaNvr, AdaptadorDahuaRetencion
from idcmd_agente.boveda import Credencial
from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Objetivo, ResultadoChequeo
from idcmd_agente.nucleo.planificador import Planificador

BLOQUEO = (Path(__file__).parent / "fixtures/dahua_nvr/cuenta_bloqueada.403.txt").read_bytes()
IP = "192.168.1.16"
CRED = Credencial(usuario="idcmd_monitor", clave=SecretStr("Clave-Lectura-2026"))
CAMARAS = {1: uuid.uuid4(), 2: uuid.uuid4()}


def _objetivo(adaptador):
    return Objetivo(dispositivo_id=uuid.uuid4(), adaptador=adaptador, direccion=IP, canales=CAMARAS)


async def _correr(clase, objetivo):
    with respx.mock() as mock:
        mock.route(host=IP).respond(403, content=BLOQUEO)
        async with httpx.AsyncClient() as cliente:
            return await clase(cliente_http=cliente, reloj=RelojFalso(T0)).chequear(objetivo, CRED)


async def test_nvr_detecta_cuenta_bloqueada():
    [r] = await _correr(AdaptadorDahuaNvr, _objetivo("dahua_nvr"))
    assert (r.estado, r.codigo) == (Estado.DESCONOCIDO, "cuenta_bloqueada")
    assert r.metricas["bloqueo_restante_s"] == 1514


async def test_retencion_informa_el_bloqueo_y_no_lo_confunde_con_busqueda():
    resultados = await _correr(AdaptadorDahuaRetencion, _objetivo("dahua_retencion"))
    assert {r.codigo for r in resultados} == {"cuenta_bloqueada"}
    assert {r.dispositivo_id for r in resultados} == set(CAMARAS.values())


class _Contador:
    """Adaptador que siempre recibe una clave rechazada y cuenta cuántas veces se le llama."""

    capacidades = frozenset()

    def __init__(self, codigo="credencial_rechazada", **metricas):
        self.llamadas = 0
        self._codigo, self._metricas = codigo, metricas

    async def chequear(self, objetivo, credencial):
        self.llamadas += 1
        return [
            ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo="x",
                estado=Estado.DESCONOCIDO,
                codigo=self._codigo,
                metricas=self._metricas,
            )
        ]


def _plan(tmp_path, adaptador, reloj):
    objetivo = Objetivo(dispositivo_id=uuid.uuid4(), adaptador="x", direccion=IP)
    maquina = MaquinaEstados()
    maquina.cargar([(objetivo.dispositivo_id, "x", Estado.OK, T0)])
    plan = Planificador(
        [objetivo],
        None,
        maquina,
        Buzon(tmp_path / "b.sqlite3"),
        Agregador(sesion=uuid.uuid4()),
        lambda: None,
        reloj=reloj,
        adaptadores={"x": adaptador},
    )
    return plan, objetivo


async def test_clave_rechazada_pausa_30_min_sin_insistir(tmp_path):
    reloj = RelojFalso(T0)
    adaptador = _Contador()
    plan, objetivo = _plan(tmp_path, adaptador, reloj)
    eventos = []
    for _ in range(29):  # 29 minutos chequeando cada minuto
        eventos += await plan.chequear(objetivo)
        reloj.avanzar(60)
    assert adaptador.llamadas == 1  # un solo intento contra el equipo
    # Aun así el problema se confirma y llega a la central.
    assert [(e.estado_nuevo, e.codigo) for e in eventos] == [
        (Estado.DESCONOCIDO, "credencial_rechazada")
    ]
    reloj.avanzar(120)  # pasaron los 30 minutos: se vuelve a intentar
    await plan.chequear(objetivo)
    assert adaptador.llamadas == 2


async def test_cuenta_bloqueada_espera_lo_que_dice_el_equipo(tmp_path):
    reloj = RelojFalso(T0)
    adaptador = _Contador("cuenta_bloqueada", bloqueo_restante_s=600)
    plan, objetivo = _plan(tmp_path, adaptador, reloj)
    await plan.chequear(objetivo)
    reloj.avanzar(600)  # 10 min: el equipo aún podría estar bloqueado (+1 min de margen)
    await plan.chequear(objetivo)
    assert adaptador.llamadas == 1
    reloj.avanzar(timedelta(minutes=1, seconds=1).total_seconds())
    await plan.chequear(objetivo)
    assert adaptador.llamadas == 2

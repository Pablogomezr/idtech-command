"""Criterio de aceptación: si el sitio pierde internet 2 horas, al reconectar no se
pierden datos de estado. Incluye un reinicio del agente en medio del corte."""

import uuid
from datetime import timedelta

import httpx
import respx
from central_falsa import URL, CentralFalsa
from conftest import T0, RelojFalso

from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.enviador import ConfigEnvio, Enviador
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Latido, Objetivo, ResultadoChequeo
from idcmd_agente.nucleo.planificador import Planificador

AGENTE = uuid.UUID("33333333-3333-4333-8333-333333333333")
CAMARA = uuid.UUID("44444444-4444-4444-8444-444444444444")
LECTORA = uuid.UUID("55555555-5555-4555-8555-555555555555")

CORTE = (20, 140)  # minutos sin internet: 2 horas
CAIDA_CAMARA = (30, 100)  # la cámara cae y vuelve DURANTE el corte
CAIDA_LECTORA = (130, 170)  # la lectora cae durante el corte y vuelve después
REINICIO_AGENTE = 80
DURACION = 180


class AdaptadorGuion:
    """Devuelve el estado que dicta un guion por minuto."""

    nombre = "guion"
    capacidades = frozenset({"disponibilidad"})

    def __init__(self, reloj):
        self._reloj = reloj

    async def chequear(self, objetivo, credencial):
        minuto = int((self._reloj() - T0).total_seconds() // 60)
        inicio, fin = CAIDA_CAMARA if objetivo.dispositivo_id == CAMARA else CAIDA_LECTORA
        estado = Estado.CAIDO if inicio <= minuto < fin else Estado.OK
        return [
            ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo="guion",
                estado=estado,
                observado_at=self._reloj(),
                metricas={"latencia_ms": 3.0} if estado == Estado.OK else {},
            )
        ]


def _arrancar_agente(ruta_buzon, reloj, objetivos):
    buzon = Buzon(ruta_buzon)
    maquina = MaquinaEstados()
    maquina.cargar(buzon.estados())
    agregador = Agregador(sesion=uuid.uuid4())

    def latido():
        return Latido(
            agente_version="0.1.0",
            reloj_agente=reloj(),
            inicio_sesion=reloj(),
            pendientes=buzon.pendientes(),
            adaptadores=["guion"],
        )

    enviador = Enviador(
        ConfigEnvio(url_central=URL, token="tok", agente_id=AGENTE),
        buzon,
        latido,
        cliente=httpx.AsyncClient(),
        reloj=reloj,
    )
    planificador = Planificador(
        objetivos,
        None,
        maquina,
        buzon,
        agregador,
        enviador.despertar,
        reloj=reloj,
        adaptadores={"guion": AdaptadorGuion(reloj)},
    )
    return buzon, agregador, enviador, planificador


async def test_dos_horas_sin_internet_no_pierden_datos(tmp_path):
    reloj = RelojFalso()
    objetivos = [
        Objetivo(dispositivo_id=CAMARA, adaptador="guion", direccion="10.0.0.10"),
        Objetivo(dispositivo_id=LECTORA, adaptador="guion", direccion="10.0.0.20"),
    ]
    central = CentralFalsa()
    ruta = tmp_path / "buzon.sqlite3"
    generados = []

    with respx.mock() as mock:
        mock.post(f"{URL}/api/v1/ingesta").mock(side_effect=central)
        buzon, agregador, enviador, planificador = _arrancar_agente(ruta, reloj, objetivos)

        for minuto in range(DURACION):
            reloj.ahora = T0 + timedelta(minutes=minuto)
            central.en_linea = not (CORTE[0] <= minuto < CORTE[1])

            if minuto == REINICIO_AGENTE:
                # El agente se detiene (p. ej. corte de energía) y vuelve a arrancar.
                for ag in agregador.vaciar():
                    buzon.encolar_agregado(ag, reloj())
                buzon.cerrar()
                buzon, agregador, enviador, planificador = _arrancar_agente(ruta, reloj, objetivos)

            for objetivo in objetivos:
                generados += await planificador.chequear(objetivo)
            planificador.mantenimiento()
            try:
                await enviador.enviar_pendientes()
            except httpx.TransportError:
                pass

    recibidos = central.eventos
    # Nada se perdió y nada llegó duplicado.
    assert sorted(e.id for e in recibidos) == sorted(e.id for e in generados)
    assert len({e.id for e in recibidos}) == len(recibidos)
    assert buzon.pendientes() == 0

    def transiciones(dispositivo):
        return [
            (e.estado_nuevo, int((e.observado_at - T0).total_seconds() // 60))
            for e in sorted(recibidos, key=lambda e: e.observado_at)
            if e.dispositivo_id == dispositivo
        ]

    # Las horas de inicio y fin son las reales, no las de la reconexión.
    assert transiciones(CAMARA) == [
        (Estado.OK, 0),
        (Estado.CAIDO, CAIDA_CAMARA[0]),
        (Estado.OK, CAIDA_CAMARA[1]),
    ]
    assert transiciones(LECTORA) == [
        (Estado.OK, 0),
        (Estado.CAIDO, CAIDA_LECTORA[0]),
        (Estado.OK, CAIDA_LECTORA[1]),
    ]

    # Los agregados horarios cubren cada minuto observado de cada equipo.
    for dispositivo in (CAMARA, LECTORA):
        muestras = sum(
            a.muestras_ok + a.muestras_caido
            for a in central.agregados
            if a.dispositivo_id == dispositivo
        )
        # Horas 08:00 y 09:00 cerradas (la de 09:00 en dos partes por el reinicio);
        # la de 10:00 sigue abierta y se enviará al cerrarse.
        assert muestras == 120

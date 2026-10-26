"""Planificador, registro de adaptadores y criterio "ninguna credencial sale del agente"."""

import asyncio
import logging
import uuid
from typing import ClassVar

import pytest
from conftest import T0
from pydantic import SecretStr

from idcmd_agente.adaptadores import AdaptadorDesconocido, descubrir, obtener, registrar
from idcmd_agente.boveda import Boveda, Credencial
from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado, Objetivo, ResultadoChequeo
from idcmd_agente.nucleo.planificador import Planificador
from idcmd_agente.nucleo.seguridad import FiltroRedaccion, GuardaSecretos

CLAVE = "Sup3r-Secreta!Milestone"


@registrar("marca_de_prueba")
class AdaptadorDePrueba:
    """Una 'marca nueva': se registra sin tocar el núcleo."""

    capacidades: ClassVar[frozenset[str]] = frozenset({"disponibilidad"})
    estado = Estado.CAIDO
    vista_credencial: Credencial | None = None

    async def chequear(self, objetivo, credencial):
        AdaptadorDePrueba.vista_credencial = credencial
        return [
            ResultadoChequeo(
                dispositivo_id=objetivo.dispositivo_id,
                chequeo="marca_de_prueba",
                estado=self.estado,
                observado_at=T0,
                metricas={"firmware": "V5.7.3"},
            )
        ]


def _armar(tmp_path, objetivos, guarda=None, boveda=None, ok_al_inicio=False, **kwargs):
    guarda = guarda or GuardaSecretos()
    maquina = MaquinaEstados()
    if ok_al_inicio:
        maquina.cargar([(o.dispositivo_id, o.adaptador, Estado.OK, T0) for o in objetivos])
    buzon = Buzon(tmp_path / "b.sqlite3", guarda)
    despertares = []
    plan = Planificador(
        objetivos,
        boveda,
        maquina,
        buzon,
        Agregador(sesion=uuid.uuid4()),
        lambda: despertares.append(1),
        reloj=lambda: T0,
        **kwargs,
    )
    return plan, buzon, despertares


def test_descubrir_registra_adaptadores_incluidos():
    assert {"icmp", "simulado"} <= set(descubrir())


def test_adaptador_desconocido_falla_al_arrancar(tmp_path, dispositivo):
    with pytest.raises(AdaptadorDesconocido):
        _armar(
            tmp_path, [Objetivo(dispositivo_id=dispositivo, adaptador="no_existe", direccion="x")]
        )


async def test_marca_nueva_funciona_sin_tocar_el_nucleo(tmp_path, dispositivo):
    assert obtener("marca_de_prueba") is AdaptadorDePrueba
    objetivo = Objetivo(dispositivo_id=dispositivo, adaptador="marca_de_prueba", direccion="x")
    plan, buzon, despertares = _armar(tmp_path, [objetivo])
    for _ in range(3):
        eventos = await plan.chequear(objetivo)
    assert [e.estado_nuevo for e in eventos] == [Estado.CAIDO]
    assert despertares == [1]
    assert buzon.pendientes() == 1


async def test_credencial_faltante_da_desconocido(tmp_path, dispositivo):
    objetivo = Objetivo(
        dispositivo_id=dispositivo,
        adaptador="marca_de_prueba",
        direccion="x",
        credencial_ref="no-cargada",
    )
    boveda = Boveda(tmp_path / "bov.json", Boveda.generar_llave())
    plan, _, _ = _armar(tmp_path, [objetivo], boveda=boveda, ok_al_inicio=True)
    for _ in range(3):
        eventos = await plan.chequear(objetivo)
    assert eventos[0].estado_nuevo == Estado.DESCONOCIDO
    assert eventos[0].codigo == "credencial_no_encontrada"


async def test_adaptador_colgado_da_desconocido(tmp_path, dispositivo):
    class Colgado:
        capacidades = frozenset()

        async def chequear(self, objetivo, credencial):
            await asyncio.sleep(10)

    objetivo = Objetivo(dispositivo_id=dispositivo, adaptador="colgado", direccion="x")
    plan, _, _ = _armar(
        tmp_path,
        [objetivo],
        ok_al_inicio=True,
        adaptadores={"colgado": Colgado()},
        timeout_chequeo_s=0.01,
    )
    for _ in range(3):
        eventos = await plan.chequear(objetivo)
    assert eventos[0].codigo == "tiempo_agotado"


async def test_ninguna_credencial_sale_del_agente(tmp_path, dispositivo, caplog):
    """Criterio de aceptación: la credencial se usa en el sitio, pero no llega al buzón
    (lo único que viaja a la central) ni a los logs, aunque un adaptador falle con ella."""
    guarda = GuardaSecretos()
    boveda = Boveda(tmp_path / "bov.json", Boveda.generar_llave(), guarda)
    boveda.guardar("vms", Credencial(usuario="svc_idcmd", clave=SecretStr(CLAVE)))

    class FallaConCredencial:
        capacidades = frozenset()

        async def chequear(self, objetivo, credencial):
            raise ConnectionError(f"login rechazado para {credencial.clave.get_secret_value()}")

    objetivos = [
        Objetivo(
            dispositivo_id=dispositivo,
            adaptador="marca_de_prueba",
            direccion="x",
            credencial_ref="vms",
        ),
        Objetivo(
            dispositivo_id=uuid.uuid4(), adaptador="falla", direccion="y", credencial_ref="vms"
        ),
    ]
    plan, buzon, _ = _armar(
        tmp_path,
        objetivos,
        guarda=guarda,
        boveda=boveda,
        adaptadores={"marca_de_prueba": AdaptadorDePrueba(), "falla": FallaConCredencial()},
    )
    # En producción el filtro va en el manejador (configurar_logs); aquí, en el de pytest.
    caplog.handler.addFilter(FiltroRedaccion(guarda))
    with caplog.at_level(logging.INFO, logger="idcmd_agente"):
        for _ in range(3):
            for objetivo in objetivos:
                await plan.chequear(objetivo)
    buzon.cerrar()

    # El adaptador sí recibió la credencial en el sitio...
    assert AdaptadorDePrueba.vista_credencial.clave.get_secret_value() == CLAVE
    # ...pero no aparece en el buzón (bytes crudos de SQLite, incluido el WAL) ni en los logs.
    for archivo in tmp_path.glob("b.sqlite3*"):
        assert CLAVE.encode() not in archivo.read_bytes()
    assert CLAVE not in caplog.text
    assert "error en el adaptador falla" in caplog.text


def test_metrica_fuera_del_vocabulario_se_rechaza(dispositivo):
    with pytest.raises(ValueError):
        ResultadoChequeo(
            dispositivo_id=dispositivo,
            chequeo="x",
            estado=Estado.OK,
            metricas={"clave": "admin123"},
        )

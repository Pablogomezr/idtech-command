from datetime import timedelta

import pytest
from conftest import T0, resultado

from idcmd_agente.nucleo.maquina_estados import MaquinaEstados
from idcmd_agente.nucleo.modelos import Estado


def _serie(maquina, dispositivo, estados, paso_s=60):
    eventos = []
    for i, estado in enumerate(estados):
        ev = maquina.procesar(resultado(dispositivo, estado, T0 + timedelta(seconds=i * paso_s)))
        if ev:
            eventos.append((i, ev))
    return eventos


def test_arranque_confirma_ok_con_una_observacion(dispositivo):
    eventos = _serie(MaquinaEstados(), dispositivo, [Estado.OK])
    assert [(i, e.estado_anterior, e.estado_nuevo) for i, e in eventos] == [
        (0, Estado.DESCONOCIDO, Estado.OK)
    ]


def test_caida_requiere_tres_fallos_y_fecha_es_la_primera_falla(dispositivo):
    ok, caido = Estado.OK, Estado.CAIDO
    eventos = _serie(MaquinaEstados(), dispositivo, [ok, caido, caido, caido])
    i, ev = eventos[-1]
    assert i == 3 and ev.estado_nuevo == caido
    # La caída empezó en la primera falla (índice 1), no cuando se confirmó.
    assert ev.observado_at == T0 + timedelta(seconds=60)


def test_falla_aislada_no_genera_evento(dispositivo):
    ok, caido = Estado.OK, Estado.CAIDO
    eventos = _serie(MaquinaEstados(), dispositivo, [ok, caido, caido, ok, caido, ok, ok])
    assert len(eventos) == 1  # solo el arranque


def test_recuperacion_requiere_dos_exitos(dispositivo):
    ok, caido = Estado.OK, Estado.CAIDO
    eventos = _serie(MaquinaEstados(), dispositivo, [ok, caido, caido, caido, ok, caido, ok, ok])
    transiciones = [(i, e.estado_nuevo) for i, e in eventos]
    assert transiciones == [(0, ok), (3, caido), (7, ok)]
    assert eventos[-1][1].observado_at == T0 + timedelta(seconds=6 * 60)


def test_degradado_es_un_estado_propio(dispositivo):
    ok, deg = Estado.OK, Estado.DEGRADADO
    eventos = _serie(MaquinaEstados(), dispositivo, [ok, deg, deg, deg])
    assert eventos[-1][1].estado_nuevo == deg


def test_cargar_estado_evita_evento_espurio_tras_reinicio(dispositivo):
    maquina = MaquinaEstados()
    maquina.cargar([(dispositivo, "icmp", Estado.CAIDO, T0)])
    assert _serie(maquina, dispositivo, [Estado.CAIDO] * 5) == []
    assert maquina.estado(dispositivo, "icmp") == Estado.CAIDO


def test_chequeos_independientes_por_dispositivo(dispositivo):
    maquina = MaquinaEstados()
    maquina.procesar(resultado(dispositivo, Estado.OK, T0, chequeo="icmp"))
    assert maquina.estado(dispositivo, "icmp") == Estado.OK
    assert maquina.estado(dispositivo, "onvif") == Estado.DESCONOCIDO


def test_confirmaciones_invalidas():
    with pytest.raises(ValueError):
        MaquinaEstados(confirmaciones_falla=0)

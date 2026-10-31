"""Reglas de disponibilidad y SLA del informe mensual (ADR 0004). Fijan lo que se cobra."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from idcmd_central.informes.calculo import (
    aplicar_sin_datos,
    combinar,
    horas_sin_datos,
    indicadores_tickets,
    linea_de_tiempo,
    medir,
    ponderada,
)

D = datetime(2026, 10, 1, 5, tzinfo=UTC)  # 1 de octubre 00:00 en Bogotá
H = timedelta(hours=1)
DIA = timedelta(days=1)


def todas_las_horas(desde, hasta):
    horas, h = set(), desde
    while h < hasta:
        horas.add(h)
        h += H
    return horas


def test_linea_de_tiempo_parte_del_ultimo_estado_conocido():
    linea = linea_de_tiempo("ok", [(D + 2 * H, "caido"), (D + 3 * H, "ok")], D, D + DIA)
    assert linea == [
        (D, D + 2 * H, "ok"),
        (D + 2 * H, D + 3 * H, "caido"),
        (D + 3 * H, D + DIA, "ok"),
    ]


def test_sin_estado_previo_es_desconocido_hasta_el_primer_evento():
    assert linea_de_tiempo(None, [(D + H, "ok")], D, D + 2 * H) == [
        (D, D + H, "desconocido"),
        (D + H, D + 2 * H, "ok"),
    ]


def test_el_equipo_cae_si_cae_cualquiera_de_sus_chequeos():
    icmp = linea_de_tiempo("ok", [], D, D + 4 * H)
    nvr = linea_de_tiempo("ok", [(D + H, "caido"), (D + 2 * H, "ok")], D, D + 4 * H)
    assert combinar([icmp, nvr], D, D + 4 * H) == [
        (D, D + H, "ok"),
        (D + H, D + 2 * H, "caido"),
        (D + 2 * H, D + 4 * H, "ok"),
    ]


def test_degradado_sigue_arriba_pero_se_informa():
    linea = linea_de_tiempo("ok", [(D + H, "degradado")], D, D + 4 * H)
    m = medir(linea, [], [], D, D + 4 * H)
    assert m.disponibilidad == 100.0
    assert m.degradado_s == 3 * 3600


def test_un_mes_con_dos_horas_caido():
    fin = D + 30 * DIA
    linea = linea_de_tiempo("ok", [(D + 10 * H, "caido"), (D + 12 * H, "ok")], D, fin)
    m = medir(linea, horas_sin_datos(todas_las_horas(D, fin), D, fin), [], D, fin)
    assert m.caido_s == 2 * 3600
    assert m.disponibilidad == pytest.approx(100 * (720 - 2) / 720)
    assert m.caidas == [(D + 10 * H, D + 12 * H)]


def test_horas_sin_agregados_son_sin_datos_no_caida():
    """El agente se apagó 5 horas: el último estado era ok, pero no hubo datos."""
    fin = D + DIA
    horas = todas_las_horas(D, fin) - {D + h * H for h in range(10, 15)}
    linea = linea_de_tiempo("ok", [], D, fin)
    m = medir(linea, horas_sin_datos(horas, D, fin), [], D, fin)
    assert m.sin_datos_s == 5 * 3600
    assert m.caido_s == 0
    assert m.disponibilidad == 100.0  # sin datos no entra en la fórmula


def test_una_caida_sin_datos_en_medio_no_se_cobra_como_caida():
    """Cae, el agente se queda sin internet y vuelve: solo se cuenta la caída observada."""
    fin = D + 6 * H
    linea = linea_de_tiempo("ok", [(D + H, "caido"), (D + 5 * H, "ok")], D, fin)
    horas = todas_las_horas(D, fin) - {D + 2 * H, D + 3 * H}
    m = medir(linea, horas_sin_datos(horas, D, fin), [], D, fin)
    assert m.caido_s == 2 * 3600 and m.sin_datos_s == 2 * 3600 and m.arriba_s == 2 * 3600


def test_mantenimiento_excluido_no_cuenta():
    fin = D + 10 * H
    linea = linea_de_tiempo("ok", [(D + 2 * H, "caido"), (D + 6 * H, "ok")], D, fin)
    m = medir(linea, [], [(D + 2 * H, D + 5 * H)], D, fin)
    assert m.excluido_s == 3 * 3600
    assert m.caido_s == 1 * 3600
    assert m.disponibilidad == pytest.approx(100 * 6 / 7)


def test_desconocido_es_sin_datos():
    linea = linea_de_tiempo("desconocido", [(D + H, "ok")], D, D + 2 * H)
    m = medir(linea, [], [], D, D + 2 * H)
    assert m.sin_datos_s == 3600 and m.disponibilidad == 100.0


def test_sin_tiempo_medible_no_hay_disponibilidad():
    m = medir(linea_de_tiempo(None, [], D, D + H), [], [], D, D + H)
    assert m.disponibilidad is None


def test_promedio_ponderado_por_criticidad():
    # crítico (peso 3) al 90 %, normal (peso 2) al 100 %, bajo (peso 1) sin medición
    assert ponderada([(90.0, 1), (100.0, 2), (None, 3)]) == pytest.approx((270 + 200) / 5)
    assert ponderada([(None, 1)]) is None


def _t(prioridad="alta", abierto=D, aceptado=None, resuelto=None, cierre=None, resp_h=4, sol_h=24):
    return {
        "prioridad": prioridad,
        "abierto_at": abierto,
        "aceptado_at": aceptado,
        "resuelto_at": resuelto,
        "cierre": cierre,
        "vence_respuesta_at": abierto + resp_h * H,
        "vence_solucion_at": abierto + sol_h * H,
    }


def test_cumplimiento_de_tickets():
    ahora = D + 10 * DIA
    tickets = [
        _t(aceptado=D + H, resuelto=D + 5 * H, cierre="verificado"),  # cumple ambos
        _t(aceptado=D + 6 * H, resuelto=D + 30 * H, cierre="verificado"),  # incumple ambos
        _t(cierre="recuperado_solo"),  # no aplica
        _t(cierre="cancelado"),  # no aplica
        _t(abierto=ahora - H),  # en curso, aún no vence: pendiente
    ]
    r = indicadores_tickets(tickets, ahora)
    assert (r["respuesta"].cumplidos, r["respuesta"].incumplidos, r["respuesta"].pendientes) == (
        1,
        1,
        1,
    )
    assert r["respuesta"].porcentaje == 50.0
    assert r["solucion"].porcentaje == 50.0
    assert r["por_prioridad"]["alta"]["no_aplica"] == 2
    assert r["mttr_s"] == pytest.approx((5 * 3600 + 30 * 3600) / 2)


def test_ticket_vencido_sin_atender_incumple():
    r = indicadores_tickets([_t()], D + 2 * DIA)
    assert r["respuesta"].incumplidos == 1 and r["solucion"].incumplidos == 1


def test_chequeo_que_deja_de_reportar_no_se_cobra_para_siempre():
    """El chequeo del NVR quedó en caído y luego se retiró de la configuración: solo cuenta la
    hora en que de verdad reportó caído; el ping siguió diciendo que el equipo estaba bien."""
    fin = D + 5 * H
    icmp = linea_de_tiempo("ok", [], D, fin)
    nvr = aplicar_sin_datos(
        linea_de_tiempo("ok", [(D + H, "caido")], D, fin),
        horas_sin_datos({D, D + H}, D, fin),  # el chequeo del NVR solo reportó 2 horas
    )
    m = medir(combinar([icmp, nvr], D, fin), [], [], D, fin)
    assert m.caido_s == 3600 and m.arriba_s == 4 * 3600


def test_sin_datos_de_ningun_chequeo_es_sin_datos_del_equipo():
    fin = D + 3 * H
    icmp = aplicar_sin_datos(linea_de_tiempo("ok", [], D, fin), horas_sin_datos({D}, D, fin))
    m = medir(combinar([icmp], D, fin), [], [], D, fin)
    assert m.sin_datos_s == 2 * 3600 and m.arriba_s == 3600

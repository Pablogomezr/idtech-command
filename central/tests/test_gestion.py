"""Indicadores de gestión: rendimiento de técnicos y confiabilidad de equipos (definiciones)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from idcmd_central.informes.calculo import Medicion
from idcmd_central.informes.gestion import (
    confiabilidad_equipos,
    reincidencias,
    rendimiento_tecnicos,
    retrabajos,
)

T = datetime(2026, 10, 1, 12, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)


def _t(n, tecnico="Ana", equipo="cam1", codigo="sin_respuesta_icmp", abierto=T, acepta=0.5,
       llega=2, resuelto=3, cierre="verificado", estado="cerrado"):  # fmt: skip
    return {
        "id": n, "tecnico": tecnico, "dispositivo_id": equipo, "codigo": codigo,
        "prioridad": "alta", "estado": estado, "cierre": cierre, "abierto_at": abierto,
        "aceptado_at": abierto + acepta * H if acepta is not None else None,
        "en_sitio_at": abierto + llega * H if llega is not None else None,
        "resuelto_at": abierto + resuelto * H if resuelto is not None else None,
        "cerrado_at": abierto + (resuelto or 0.5) * H + 10 * timedelta(minutes=1),
        "vence_respuesta_at": abierto + 4 * H, "vence_solucion_at": abierto + 24 * H,
    }  # fmt: skip


def test_retrabajo_es_la_misma_falla_en_el_mismo_equipo_dentro_de_30_dias():
    primero = _t(1)
    vuelve = _t(2, abierto=T + 10 * D)  # misma falla, mismo equipo: retrabajo del 1
    otra_falla = _t(3, codigo="tamper_activo", abierto=T + 12 * D)
    tarde = _t(4, abierto=T + 60 * D)  # pasaron más de 30 días desde el 2: no es retrabajo
    assert retrabajos([primero, vuelve, otra_falla, tarde]) == {1}


def test_reincidencia_es_cualquier_falla_poco_despues_del_cierre_anterior():
    tickets = [_t(1), _t(2, codigo="tamper_activo", abierto=T + 5 * D), _t(3, abierto=T + 90 * D)]
    assert reincidencias(tickets) == {2}


def test_rendimiento_de_un_tecnico():
    ahora = T + 100 * D
    tickets = [
        _t(1),  # reparado, pero la falla volvió (retrabajo)
        _t(2, abierto=T + 10 * D),  # reparado a la primera
        _t(3, abierto=T + 50 * D, acepta=6, resuelto=30),  # fuera de plazo; la falla volvió (4)
        _t(4, abierto=T + 70 * D, acepta=None, llega=None, resuelto=None, cierre="recuperado_solo"),
        _t(5, tecnico="Luis", equipo="nvr", abierto=T + 3 * D),
    ]
    filas = {f["tecnico"]: f for f in rendimiento_tecnicos(tickets, tickets, {3}, ahora)}
    ana = filas["Ana"]
    assert ana["servicios"] == 4 and ana["reparados"] == 3 and ana["atendidos"] == 3
    assert ana["retrabajos"] == 2 and ana["reaperturas"] == 1  # el 1 (por el 2) y el 3 (por el 4)
    assert ana["primera_pct"] == pytest.approx(100 / 3)  # solo el 2 quedó bien a la primera
    assert ana["respuesta_pct"] == pytest.approx(200 / 3)  # el 4 (se recuperó solo) no cuenta
    assert ana["respuesta_s"] == pytest.approx((0.5 + 0.5 + 6) / 3 * 3600)
    assert filas["Luis"]["primera_pct"] == 100.0
    assert list(filas) == ["Ana", "Luis"]  # el de más servicios primero


def test_confiabilidad_de_equipos():
    m = Medicion(arriba_s=29 * 86400, caido_s=86400,
                 caidas=[(T, T + 12 * H), (T + 10 * D, T + 10 * D + 12 * H)])  # fmt: skip
    equipos = [{"id": "cam1", "nombre": "Cámara 1", "tipo": "camara"},
               {"id": "cam2", "nombre": "Cámara 2", "tipo": "camara"}]  # fmt: skip
    tickets = [_t(1), _t(2, abierto=T + 10 * D, llega=None)]
    filas = confiabilidad_equipos(equipos, {"cam1": m}, tickets, tickets)
    cam1, cam2 = filas
    assert cam1["caidas"] == 2 and cam1["mtbf_s"] == 15 * 86400  # falla cada 15 días
    assert cam1["mttr_s"] == 12 * 3600 and cam1["ultima_caida"] == T + 10 * D
    assert cam1["servicios"] == 2 and cam1["visitas"] == 1 and cam1["reincidencias"] == 1
    assert cam2["medido"] is False and cam2["caidas"] == 0 and cam2["mtbf_s"] is None

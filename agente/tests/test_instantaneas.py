from datetime import timedelta

from conftest import T0, resultado

from idcmd_agente.nucleo.instantaneas import SelectorInstantaneas
from idcmd_agente.nucleo.modelos import Estado


def _r(dispositivo, minutos, **metricas):
    return resultado(
        dispositivo,
        Estado.OK,
        T0 + timedelta(minutes=minutos),
        chequeo="hikvision_acceso",
        **metricas,
    )


def test_primera_lectura_se_envia(dispositivo):
    assert SelectorInstantaneas().evaluar(_r(dispositivo, 0, usuarios_registrados=12))


def test_sin_cambios_estables_no_se_reenvia_antes_de_una_hora(dispositivo):
    sel = SelectorInstantaneas()
    sel.evaluar(_r(dispositivo, 0, usuarios_registrados=12, latencia_ms=3.0))
    # La latencia y el desfase cambian siempre: no cuentan como cambio.
    assert sel.evaluar(_r(dispositivo, 1, usuarios_registrados=12, latencia_ms=9.0)) is None
    assert sel.evaluar(_r(dispositivo, 61, usuarios_registrados=12, latencia_ms=4.0))


def test_cambio_en_usuarios_se_envia_de_inmediato(dispositivo):
    sel = SelectorInstantaneas()
    sel.evaluar(_r(dispositivo, 0, usuarios_registrados=12))
    inst = sel.evaluar(_r(dispositivo, 1, usuarios_registrados=13))
    assert inst is not None and inst.metricas["usuarios_registrados"] == 13


def test_sin_metricas_no_hay_instantanea(dispositivo):
    assert SelectorInstantaneas().evaluar(_r(dispositivo, 0)) is None


def test_id_determinista_para_reenvio_idempotente(dispositivo):
    a = SelectorInstantaneas().evaluar(_r(dispositivo, 0, modelo="DS-K2602T"))
    b = SelectorInstantaneas().evaluar(_r(dispositivo, 0, modelo="DS-K2602T"))
    assert a.id == b.id

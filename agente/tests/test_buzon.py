import uuid
from datetime import timedelta

import pytest
from conftest import T0

from idcmd_agente.nucleo.agregador import Agregador
from idcmd_agente.nucleo.buzon import Buzon
from idcmd_agente.nucleo.modelos import Estado, EventoEstado
from idcmd_agente.nucleo.seguridad import GuardaSecretos, SecretoDetectado


def _evento(dispositivo, nuevo=Estado.CAIDO, minuto=0):
    return EventoEstado(
        dispositivo_id=dispositivo,
        chequeo="icmp",
        estado_anterior=Estado.OK,
        estado_nuevo=nuevo,
        observado_at=T0 + timedelta(minutes=minuto),
    )


def _agregado(dispositivo, hora):
    from conftest import resultado

    ag = Agregador(sesion=uuid.uuid4())
    ag.registrar(resultado(dispositivo, Estado.OK, T0 + timedelta(hours=hora)))
    return ag.vaciar()[0]


def test_sobrevive_a_un_reinicio(tmp_path, dispositivo):
    ruta = tmp_path / "buzon.sqlite3"
    buzon = Buzon(ruta)
    ev = _evento(dispositivo)
    buzon.registrar_transicion(ev, T0)
    buzon.cerrar()

    buzon = Buzon(ruta)
    assert [i.id for i in buzon.tomar_lote(10)] == [str(ev.id)]
    assert buzon.estados() == [(dispositivo, "icmp", Estado.CAIDO, ev.observado_at)]


def test_encolar_dos_veces_no_duplica(tmp_path, dispositivo):
    buzon = Buzon(tmp_path / "b.sqlite3")
    ev = _evento(dispositivo)
    buzon.registrar_transicion(ev, T0)
    buzon.registrar_transicion(ev, T0)
    assert buzon.pendientes() == 1


def test_eventos_salen_antes_que_agregados(tmp_path, dispositivo):
    buzon = Buzon(tmp_path / "b.sqlite3")
    buzon.encolar_agregado(_agregado(dispositivo, 0), T0)
    buzon.registrar_transicion(_evento(dispositivo), T0)
    assert [i.tipo for i in buzon.tomar_lote(10)] == ["evento", "agregado"]


def test_marcar_enviados_y_reenviar(tmp_path, dispositivo):
    buzon = Buzon(tmp_path / "b.sqlite3")
    buzon.registrar_transicion(_evento(dispositivo), T0)
    buzon.marcar_enviados([i.id for i in buzon.tomar_lote(10)], T0)
    assert buzon.pendientes() == 0
    assert buzon.reenviar_desde(T0 - timedelta(hours=1)) == 1
    assert buzon.pendientes() == 1


def test_purga_conserva_eventos_y_descarta_agregados_viejos(tmp_path, dispositivo):
    buzon = Buzon(tmp_path / "b.sqlite3")
    for hora in range(5):
        buzon.encolar_agregado(_agregado(dispositivo, hora), T0)
    for minuto in range(3):
        buzon.registrar_transicion(_evento(dispositivo, minuto=minuto), T0)
    buzon.purgar(T0, max_pendientes=4)
    tipos = [i.tipo for i in buzon.tomar_lote(100)]
    assert tipos.count("evento") == 3
    assert tipos.count("agregado") == 1


def test_purga_borra_enviados_vencidos(tmp_path, dispositivo):
    buzon = Buzon(tmp_path / "b.sqlite3")
    buzon.registrar_transicion(_evento(dispositivo), T0)
    buzon.marcar_enviados([i.id for i in buzon.tomar_lote(10)], T0)
    assert buzon.purgar(T0 + timedelta(days=6)) == 0
    assert buzon.purgar(T0 + timedelta(days=8)) == 1


def test_rechaza_contenido_con_secreto(tmp_path, dispositivo):
    guarda = GuardaSecretos()
    guarda.registrar("icmp")  # aparece en el campo chequeo del evento
    buzon = Buzon(tmp_path / "b.sqlite3", guarda)
    with pytest.raises(SecretoDetectado):
        buzon.registrar_transicion(_evento(dispositivo), T0)
    assert buzon.pendientes() == 0
    assert buzon.estados() == []

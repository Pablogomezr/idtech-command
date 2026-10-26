import json
import logging
import stat

import pytest
from pydantic import SecretStr

from idcmd_agente.boveda import Boveda, BovedaError, Credencial
from idcmd_agente.nucleo.seguridad import FiltroRedaccion, GuardaSecretos

CLAVE = 'Cl@ve"Secreta\\2026'


@pytest.fixture
def boveda(tmp_path):
    return Boveda(tmp_path / "boveda.json", Boveda.generar_llave())


def test_ida_y_vuelta(boveda):
    boveda.guardar("milestone-principal", Credencial(usuario="svc_idcmd", clave=SecretStr(CLAVE)))
    cred = boveda.obtener("milestone-principal")
    assert cred.usuario == "svc_idcmd"
    assert cred.clave.get_secret_value() == CLAVE
    assert boveda.listar() == ["milestone-principal"]


def test_archivo_no_contiene_texto_plano_y_es_privado(boveda, tmp_path):
    boveda.guardar("biostar", Credencial(usuario="lector_estado", clave=SecretStr(CLAVE)))
    contenido = (tmp_path / "boveda.json").read_text()
    assert CLAVE not in contenido and "lector_estado" not in contenido
    assert stat.S_IMODE((tmp_path / "boveda.json").stat().st_mode) == 0o600


def test_la_clave_no_se_imprime():
    cred = Credencial(usuario="u", clave=SecretStr(CLAVE))
    assert CLAVE not in repr(cred) and CLAVE not in str(cred)


def test_llave_incorrecta_falla(tmp_path):
    ruta = tmp_path / "boveda.json"
    Boveda(ruta, Boveda.generar_llave()).guardar(
        "a", Credencial(usuario="u", clave=SecretStr(CLAVE))
    )
    with pytest.raises(BovedaError):
        Boveda(ruta, Boveda.generar_llave()).obtener("a")


def test_mover_cifrado_entre_entradas_se_detecta(boveda, tmp_path):
    boveda.guardar("a", Credencial(usuario="u", clave=SecretStr(CLAVE)))
    boveda.guardar("b", Credencial(usuario="u", clave=SecretStr("otra-clave")))
    ruta = tmp_path / "boveda.json"
    datos = json.loads(ruta.read_text())
    datos["entradas"]["b"] = datos["entradas"]["a"]
    ruta.write_text(json.dumps(datos))
    with pytest.raises(BovedaError):
        boveda.obtener("b")


def test_referencia_invalida(boveda):
    with pytest.raises(BovedaError):
        boveda.guardar("../escape", Credencial(usuario="u", clave=SecretStr(CLAVE)))


def test_llave_en_archivo(tmp_path):
    ruta = tmp_path / "llave"
    llave = Boveda.generar_llave()
    Boveda.escribir_llave(ruta, llave)
    assert Boveda.leer_llave(ruta) == llave
    assert stat.S_IMODE(ruta.stat().st_mode) == 0o600
    with pytest.raises(BovedaError):
        Boveda.escribir_llave(ruta, llave)


def test_al_abrir_registra_claves_en_la_guarda(tmp_path):
    llave = Boveda.generar_llave()
    Boveda(tmp_path / "b.json", llave).guardar("a", Credencial(usuario="u", clave=SecretStr(CLAVE)))
    guarda = GuardaSecretos()
    Boveda(tmp_path / "b.json", llave, guarda)
    assert guarda.contiene_secreto(f"error conectando con clave {CLAVE}")
    # También en su forma escapada dentro de un JSON.
    assert guarda.contiene_secreto(json.dumps({"x": CLAVE}))


def test_logs_redactan_secretos_incluidas_trazas(caplog):
    guarda = GuardaSecretos()
    guarda.registrar(CLAVE)
    logger = logging.getLogger("prueba.redaccion")
    logger.addFilter(FiltroRedaccion(guarda))
    with caplog.at_level(logging.INFO):
        try:
            raise RuntimeError(f"http://admin:{CLAVE}@10.0.0.5 rechazó la conexión")
        except RuntimeError:
            logger.exception("fallo con %s", CLAVE)
    assert CLAVE not in caplog.text
    assert "***" in caplog.text

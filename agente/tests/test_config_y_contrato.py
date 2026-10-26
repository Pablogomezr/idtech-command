import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from idcmd_agente.config import ConfigAgente, cargar_config
from idcmd_agente.nucleo.modelos import LoteIngesta

RAIZ = Path(__file__).resolve().parents[2]
ESQUEMA = RAIZ / "contrato" / "ingesta-v1.schema.json"


def _config(intervalo_s=60, **deteccion):
    return {
        "agente_id": "33333333-3333-4333-8333-333333333333",
        "central": {"url": "https://central.prueba", "archivo_token": "/run/secrets/t"},
        "deteccion": deteccion,
        "objetivos": [
            {
                "dispositivo_id": "44444444-4444-4444-8444-444444444444",
                "adaptador": "icmp",
                "direccion": "10.0.0.10",
                "intervalo_s": intervalo_s,
            }
        ],
    }


def test_config_por_defecto_cumple_deteccion_en_5_minutos():
    ConfigAgente.model_validate(_config())


def test_config_que_no_cumple_deteccion_se_rechaza():
    with pytest.raises(ValidationError, match="detección"):
        ConfigAgente.model_validate(_config(intervalo_s=90))
    with pytest.raises(ValidationError, match="detección"):
        ConfigAgente.model_validate(_config(confirmaciones_falla=5))


def test_config_rechaza_campos_desconocidos():
    datos = _config()
    datos["central"]["clave"] = "no-debe-ir-aqui"
    with pytest.raises(ValidationError):
        ConfigAgente.model_validate(datos)


def test_config_ejemplo_es_valida():
    cargar_config(RAIZ / "agente" / "config.ejemplo.yaml")


def test_esquema_del_contrato_esta_al_dia():
    """Si falla: correr `uv run python scripts/generar_contrato.py` y versionar el cambio."""
    assert json.loads(ESQUEMA.read_text()) == LoteIngesta.model_json_schema()

"""Regenera contrato/ingesta-v1.schema.json desde los modelos del agente."""

import json
from pathlib import Path

from idcmd_agente.nucleo.modelos import LoteIngesta

destino = Path(__file__).resolve().parents[2] / "contrato" / "ingesta-v1.schema.json"
destino.write_text(json.dumps(LoteIngesta.model_json_schema(), indent=2, ensure_ascii=False) + "\n")
print(f"escrito {destino}")

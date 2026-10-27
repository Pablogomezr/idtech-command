"""Instantáneas de salud: cuándo enviar las métricas vigentes de un chequeo a la central.

Los eventos solo viajan cuando cambia el ESTADO. Pero hay cambios importantes sin cambio
de estado: un panel que pasa de 12 a 13 usuarios, una lectora que baja de "tarjeta + huella"
a "solo tarjeta", un firmware actualizado. Regla:

* se envía cuando cambia cualquier métrica no volátil (ver METRICAS_VOLATILES);
* y al menos cada `refresco` (1 h), para que el valor actual en la central esté fresco
  (retención, uso de disco).

No se persiste entre reinicios: tras reiniciar se reenvía la primera instantánea de cada
chequeo, lo que es inofensivo (la central solo registra cambios reales).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .modelos import METRICAS_VOLATILES, Instantanea, ResultadoChequeo

_NS_INSTANTANEAS = uuid.UUID("0b5b3c7e-2f7d-4f43-9a51-9a3c6f1d2e88")
REFRESCO = timedelta(hours=1)


class SelectorInstantaneas:
    def __init__(self, refresco: timedelta = REFRESCO):
        self._refresco = refresco
        self._ultima: dict[tuple[uuid.UUID, str], tuple[dict, datetime]] = {}

    def evaluar(self, resultado: ResultadoChequeo) -> Instantanea | None:
        if not resultado.metricas:
            return None
        clave = (resultado.dispositivo_id, resultado.chequeo)
        estables = {k: v for k, v in resultado.metricas.items() if k not in METRICAS_VOLATILES}
        previa = self._ultima.get(clave)
        if (
            previa is not None
            and previa[0] == estables
            and resultado.observado_at - previa[1] < self._refresco
        ):
            return None
        self._ultima[clave] = (estables, resultado.observado_at)
        return Instantanea(
            id=uuid.uuid5(
                _NS_INSTANTANEAS,
                f"{resultado.dispositivo_id}|{resultado.chequeo}|{resultado.observado_at.isoformat()}",
            ),
            dispositivo_id=resultado.dispositivo_id,
            chequeo=resultado.chequeo,
            estado=resultado.estado,
            observado_at=resultado.observado_at,
            metricas=resultado.metricas,
        )

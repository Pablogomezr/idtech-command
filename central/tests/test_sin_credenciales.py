"""Criterio de aceptación: ninguna credencial de dispositivo existe en la base central.

Se verifica sobre el catálogo real de Postgres: ninguna columna con nombre de secreto,
salvo una lista cerrada y justificada que no corresponde a equipos de clientes.
"""

import re

PATRON_SECRETO = re.compile(
    r"(clave|contrasena|password|passwd|secreto|secret|credencial|token|usuario_equipo|pin)"
)

# Columnas permitidas, cada una con su razón.
PERMITIDAS = {
    ("agentes", "token_hash"): "SHA-256 del token del agente; el token no se guarda",
    ("usuarios", "hash_clave"): "bcrypt de la clave del portal; se verifica solo en la base",
    ("usuarios", "totp_cifrado"): "segundo factor del portal, cifrado",
    ("sesiones", "token_hash"): "SHA-256 del token de sesión del portal; el token no se guarda",
    ("dispositivos", "credencial_ref"): "alias que el agente resuelve en su bóveda local",
}


def test_ninguna_columna_guarda_credenciales_de_equipos(dueno, datos):
    columnas = dueno.execute(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'idcmd'"
    ).fetchall()
    sospechosas = {(t, c) for t, c in columnas if PATRON_SECRETO.search(c)}
    assert sospechosas - set(PERMITIDAS) == set()


def test_credencial_ref_no_admite_algo_con_forma_de_clave(dueno, datos):
    import psycopg
    import pytest

    with pytest.raises(psycopg.errors.CheckViolation):
        dueno.execute(
            "UPDATE idcmd.dispositivos SET credencial_ref = 'Admin#2026!' WHERE id = %s",
            (datos["camara_sitio_a1"],),
        )


def test_metricas_guardadas_no_tienen_claves_fuera_del_vocabulario(dueno, datos):
    from idcmd_central.contrato import METRICAS_PERMITIDAS

    claves = dueno.execute(
        "SELECT DISTINCT jsonb_object_keys(metricas) FROM idcmd.eventos_estado"
        " UNION SELECT DISTINCT jsonb_object_keys(metricas) FROM idcmd.estado_actual"
    ).fetchall()
    assert {c for (c,) in claves} <= set(METRICAS_PERMITIDAS)

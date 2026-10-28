"""Lectura para el operador: frases, ✓/✗, barras y qué hacer (valores reales de la oficina)."""

from idcmd_central.portal.interpretacion import interpretar

PANEL_OFICINA = {
    "modelo": "DS-K2602T", "desfase_reloj_s": -165.2, "ntp_activo": False,
    "zona_horaria_utc_h": -5.0, "tamper_activo": True, "bateria_baja": True,
    "lectoras_desconectadas": 0, "tarjetas_registradas": 1, "modos_verificacion": "7,7,7,7",
    "usuarios_registrados": 2,
}  # fmt: skip


def test_panel_con_varias_novedades():
    lec = interpretar("hikvision_acceso", "degradado", "tamper_activo", PANEL_OFICINA)
    malos = [p.texto for p in lec.puntos if p.nivel == "mal"]
    assert lec.titular == "3 novedades en el panel"
    assert malos == ["Tapa abierta (tamper)", "Sin batería de respaldo o batería baja",
                     "Reloj atrasado 2 min 45 s"]  # fmt: skip
    assert any("huella o tarjeta" in p.texto for p in lec.puntos)
    assert len(lec.acciones) == 3  # tapa, batería y NTP: una acción por problema
    assert "Modelo" in lec.tecnicos  # los números crudos quedan en detalles técnicos


def test_nvr_sano_muestra_barra_de_disco_y_verificaciones():
    lec = interpretar("dahua_nvr", "ok", None, {
        "almacenamiento_uso_pct": 53.1, "almacenamiento_total_gb": 3951, "discos_total": 1,
        "discos_con_error": 0, "desfase_reloj_s": 3.7,
    })  # fmt: skip
    assert lec.titular == "Grabador funcionando"
    assert lec.barras[0].texto == "53,1 % usado de 4,0 TB"
    assert [p.texto for p in lec.puntos] == ["1 disco(s) sin errores", "Hora correcta"]
    assert lec.acciones == []


def test_retencion_frente_a_lo_contratado():
    metricas = {"retencion_real_dias": 120.3, "retencion_proyectada_dias": 226.6}
    lec = interpretar("dahua_retencion", "ok", None, metricas, 30)
    assert lec.titular == "Guarda 120,3 días de video"
    assert lec.barras[0].nivel == "bien" and lec.barras[0].porcentaje == 100


def test_camara_que_no_graba_no_se_pinta_verde_aunque_tenga_video_viejo():
    lec = interpretar("dahua_retencion", "degradado", "sin_grabacion_reciente",
                    {"retencion_real_dias": 120.3}, 30)  # fmt: skip
    assert lec.titular.startswith("No está grabando")
    assert lec.barras[0].nivel == "atencion" and "✓" not in lec.barras[0].texto
    assert lec.acciones and "programación de grabación" in lec.acciones[0]


def test_canal_vacio_dice_que_hacer():
    lec = interpretar("dahua_nvr_canal", "caido", "canal_sin_camara", {})
    assert lec.titular == "Canal sin cámara asignada"
    assert "Auditoría" in lec.acciones[0]


def test_ping():
    assert interpretar("icmp", "ok", None, {"latencia_ms": 1.2}).titular.startswith("Responde")
    assert interpretar("icmp", "caido", "sin_respuesta_icmp", {}).titular == "No responde en la red"

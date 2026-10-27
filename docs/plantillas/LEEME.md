# Plantilla de la base de conocimiento de fallas

`base_conocimiento.csv` es la carga inicial. Una fila por combinación de falla, tipo de
equipo y marca. Las filas con `tipo_equipo` y `marca` vacíos son genéricas; el sistema
muestra la más específica que coincida (marca + tipo > tipo > genérica).

| Columna | Contenido |
|---|---|
| `codigo_falla` | Identificador estable, `[a-z0-9_]` (p. ej. `falla_disco`). Se repite en filas de la misma falla. |
| `nombre_falla` | Cómo se muestra en el portal y en el informe. |
| `categoria` | `red`, `energia`, `grabacion`, `almacenamiento`, `hardware`, `configuracion`, `control_acceso`. |
| `disparadores` | Códigos que emite el agente y que clasifican el ticket automático, separados por `\|`. |
| `tipo_equipo` | `camara`, `nvr`, `servidor_vms`, `lectora`, `controladora`, `switch`… o vacío. |
| `marca` | `hikvision`, `dahua`, `milestone`, `suprema`, `zkteco`, `axis`, `hanwha`… o vacío. |
| `causas_probables` | Separadas por `\|`, de la más a la menos frecuente. |
| `pasos_sugeridos` | Separados por `\|`, en el orden en que el técnico debe hacerlos. |

Las filas incluidas son ejemplos para mostrar el formato; reemplácelas con su experiencia.
Códigos que el agente emite hoy: `sin_respuesta_icmp`, `perdida_paquetes`, `sin_respuesta_http`,
`credencial_rechazada`, `credencial_requerida`, `tamper_activo`, `lectora_desconectada`,
`bateria_baja`, `reloj_desfasado`, `disco_con_error`, `sin_disco`, `camara_desconectada_nvr`, `canal_sin_camara`,
`sin_grabaciones`, `busqueda_no_soportada`, `zona_horaria_incorrecta`, `capacidad_reducida`,
`disco_casi_lleno`, `retencion_proyectada_insuficiente`, `cuenta_bloqueada`,
`retencion_insuficiente`, `credencial_no_encontrada`, `tiempo_agotado`, `error_adaptador`.

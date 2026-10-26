# IDtech Command — guía para Claude y para quien mantenga el proyecto

Producto de **Ingeniería y Desarrollo IDtech SAS** (Barranquilla): portal de monitoreo de
salud **multimarca** de CCTV y control de acceso, gestión de correctivos y **informe mensual
de SLA**. IDtech es integrador, no fabricante: el valor está en la evidencia de SLA y en los
correctivos facturables, no en el hardware.

Lo mantiene **una sola persona con ayuda de IA**. Ante la duda: menos piezas, menos
dependencias, código aburrido y probado.

## Principios que NO se negocian

1. **Solo lectura sobre los equipos del cliente.** Nada de firmware, cambio de contraseñas,
   configuración por lotes, video en vivo ni reproducción. Los adaptadores REST usan
   `ClienteSoloLectura` con lista blanca explícita. Única excepción futura (Fase 2): reinicio
   remoto firmado, habilitado localmente por equipo, con doble confirmación y auditoría
   (ADR 0006).
2. **Las credenciales de los equipos viven solo en el agente**, cifradas en la bóveda. Nunca
   en la central, ni en el YAML, ni en logs, ni en payloads. Se cargan solo por la consola
   local (`idcmd credenciales guardar`).
3. **Conexión solo saliente** desde el sitio (HTTPS 443, con proxy y CA corporativa si hace
   falta). Ningún puerto abierto en el sitio.
4. **Datos en Colombia**: central en Oracle Cloud región Bogotá, incluidos archivos y
   respaldos. No usar nubes de fabricantes (p. ej. DoLynk/Dahua) como fuente de datos.
5. **Minimización**: correos, Telegram y web push llevan solo nombre del equipo, sitio,
   estado y un enlace. Nunca IPs ni detalles técnicos. Nada de eventos de acceso/marcaciones.
6. **No se pierde ningún dato de estado**: todo pasa por el buzón SQLite antes de enviarse.

## Decisiones (ver `docs/adr/`)

| ADR | Decisión |
|---|---|
| 0001 | Agente propio en Python, no Zabbix (credenciales pasarían por la central; TCP 10051 no cruza proxies). |
| 0002 | Central en OCI Bogotá: una VM con Docker compose, Postgres 16 propio, auth propia, Object Storage Bogotá. |
| 0003 | Portal FastAPI + Jinja + HTMX renderizado en servidor + PWA para técnicos. Un solo lenguaje. |
| 0004 | Disponibilidad y reloj SLA calculados desde eventos; reverificación antes del cierre. |
| 0005 | Seguridad del agente: bóveda AES-256-GCM, guarda de secretos, contenedor endurecido. |
| 0006 | Principio de solo lectura y la excepción del reinicio remoto (Fase 2). |
| 0007 | Idioma: identificadores y documentación en español, sin tildes ni ñ en el código. |

Supuestos aprobados por Pablo (P1–P10): `docs/supuestos.md`.

## Estructura

```
agente/      Python 3.12, imagen Docker única del sitio (idcmd_agente)
  src/idcmd_agente/nucleo/       modelos, maquina_estados, buzon, enviador, planificador,
                                 agregador, seguridad  — NO conoce marcas
  src/idcmd_agente/boveda/       credenciales cifradas
  src/idcmd_agente/adaptadores/  base (registro), http_seguro, icmp, simulado, ...
contrato/    ingesta-v1.schema.json, generado desde agente/.../modelos.py
central/     FastAPI: ingesta + portal + worker (idcmd_central)
  migraciones/                   SQL plano, una vez y en orden (idcmd-central migrar)
  src/idcmd_central/db.py        transacción con SET LOCAL ROLE + app.usuario_id/agente_id
  src/idcmd_central/ingesta/     POST /api/v1/ingesta
  src/idcmd_central/contrato.py  espejo del contrato; una prueba lo compara con el esquema
docs/        adr/, runbooks/, plantillas/, supuestos.md
despliegue/  Caddy, compose de la central, aprovisionamiento del mini PC
```

## Comandos

```bash
cd agente
uv sync                                  # dependencias
uv run pytest -q                         # pruebas
uv run ruff check . && uv run ruff format --check .
uv run python scripts/generar_contrato.py  # tras cambiar modelos del contrato
docker build -t idcmd-agente:dev .

cd central                               # requiere Postgres 16 (ver tests/conftest.py)
docker run -d --name idcmd-pg -e POSTGRES_PASSWORD=pruebas -p 55432:5432 postgres:16
uv sync && uv run pytest -q
IDCMD_DSN_ADMIN=... uv run idcmd-central migrar
IDCMD_DSN_ADMIN=... uv run idcmd-central agentes crear --sitio <uuid> --nombre "..."
```

## Base de datos central (reglas)

- Roles: `idcmd_app` (portal) e `idcmd_ingesta` (agentes), sin login, sin BYPASSRLS, no
  dueños de tablas. El usuario de login es miembro NOINHERIT: sin `SET LOCAL ROLE` no tiene
  permisos. La aplicación **nunca** se conecta con el rol dueño.
- **Toda tabla nueva lleva RLS** (`test_todas_las_tablas_tienen_rls` lo exige) y, si tiene
  datos de un sitio, `cliente_id` + `sitio_id` con llave foránea compuesta.
- Visibilidad: `idcmd.sitios_visibles()` (admin todo; técnico sus asignaciones vigentes;
  cliente su alcance cliente/regional/sitio). Escritura de inventario: solo Admin.
- Hashes de tokens y claves del portal: el rol `idcmd_app` puede escribirlos, nunca leerlos.
- `auditoria_accesos` solo admite INSERT.
- Migraciones: nunca editar una ya aplicada en producción; crear la siguiente.
- `agregados_hora` sin particionar (volumen manejable); se conservan 90 días.

## Convenciones

- Identificadores en español sin tildes ni ñ: `retencion_real_dias`, `anio` (no `año`).
- Fechas siempre con zona horaria y en UTC dentro del sistema; se muestran en America/Bogota.
- Todo modelo que cruza una frontera (agente→central) usa `extra="forbid"`.
- Los adaptadores traducen al **vocabulario común** (`METRICAS_PERMITIDAS` en
  `nucleo/modelos.py`). Agregar una métrica nueva es un cambio de contrato: actualizar el
  esquema y la central.
- Códigos (`codigo`, `chequeo`): `[a-z0-9_]`, máx. 64. Son los disparadores de la base de
  conocimiento de fallas.
- Comentarios y docstrings explican el *porqué*, no repiten el código.

## Cómo agregar una marca (criterio: sin tocar el núcleo)

1. Crear `agente/src/idcmd_agente/adaptadores/<marca>.py` con una clase
   `@registrar("<marca>")` que implemente `chequear(objetivo, credencial)`.
2. Usar `ClienteSoloLectura` con la lista blanca mínima de operaciones de lectura.
3. Traducir al vocabulario común; nunca devolver credenciales ni datos de personas.
4. Pruebas con respuestas reales grabadas en `tests/fixtures/<marca>/` (por versión de
   firmware) y prueba de que todo lo que no está en la lista blanca se bloquea.

## Reglas para cambios

- Cambiar el contrato de ingesta = regenerar `contrato/ingesta-v1.schema.json`; si rompe
  compatibilidad, crear v2 (los agentes en campo no se actualizan todos a la vez).
- La máquina de estados y las fórmulas de SLA son el corazón del negocio: no se cambian sin
  pruebas que fijen el comportamiento nuevo.
- No agregar dependencias sin justificarlo; cada una es mantenimiento para una persona.
- Nada de datos reales de clientes (IPs, nombres de sitios) en el repositorio ni en fixtures.

## Estado

| Etapa | Contenido | Estado |
|---|---|---|
| E1 | Núcleo del agente (este código), contrato v1 | ✅ Entrega 2 |
| E1 | Central: esquema Postgres + RLS, API de ingesta | ✅ Entrega 3 |
| E1 | Portal mínimo con login MFA y estado en vivo | ⏳ Entrega 4 |
| E2–E6 | Adaptadores, operación, PWA, piloto, SLA e informe | Pendiente |

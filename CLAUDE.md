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
   estado y un enlace. Nunca IPs ni detalles técnicos. **Ni datos ni eventos de personas**
   (marcaciones, listados de usuarios, tarjetas, rostros); sí **totales** (usuarios y
   tarjetas registrados) y **configuración de seguridad** (modo de verificación). ADR 0008.
6. **No se pierde ningún dato de estado**: todo pasa por el buzón SQLite antes de enviarse.
7. **El agente nunca bloquea una cuenta de los equipos.** Usa su cuenta propia
   (`idcmd_monitor`), nunca `admin`; con `credencial_rechazada` pausa el objetivo 30 min y con
   `cuenta_bloqueada` espera lo que indica el equipo (planificador). Insistir con una clave
   vieja dejaría a los operadores del cliente sin acceso. Excepción: equipos que solo admiten
   `admin` (DS-K2602T) usan `cuenta_compartida: true` + `ip_agente` (ADR 0009).

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
| 0008 | Totales de usuarios y configuración de seguridad permitidos; datos de personas no. |
| 0009 | Auditoría de accesos administrativos (cuenta e IP); solo operaciones, nunca marcaciones. |
| 0010 | Servicio: la falla abre el ticket sola, asignación por zona con escalamiento, SLA 24/7, cierre verificado, avisos correo/Telegram, seguimiento por enlace firmado. |

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
  src/idcmd_central/portal/      ingreso (clave + TOTP), estado en vivo (HTMX), auditoría
  src/idcmd_central/laboratorio.py  inventario YAML -> central + config del agente
  src/idcmd_central/servicio/    worker de tickets (reglas, apertura, escalamiento, SLA, avisos),
                                 base de conocimiento (copia de docs/plantillas) y almacén de fotos
  src/idcmd_central/portal/campo.py  app del técnico: llegada, pasos, fotos, firma, acta, PWA
  src/idcmd_central/informes/    informe mensual de SLA: calculo.py (reglas ADR 0004, puras),
                                 informe.py (datos -> resumen), emitir.py (día 1, 6 a. m.), PDF
                                 (WeasyPrint), gráfico SVG; portal/informes.py (portal y enlace)
  src/idcmd_central/portal/servicios.py  reporte de servicios por equipo/falla/técnico + CSV
  src/idcmd_central/portal/gestion.py    rendimiento de técnicos y confiabilidad de equipos + CSV
                                 (definiciones puras en informes/gestion.py)
  src/idcmd_central/portal/admin.py      sitios, usuarios, técnicos y asignaciones (solo admin;
                                 funciones SECURITY DEFINER de la migración 0010)
  src/idcmd_central/portal/apariencia.py  figura del equipo según la referencia (bala, domo, PTZ,
                                 grabador, panel...) + modelos.py (foto por modelo, solo admin,
                                 WebP sin EXIF en idcmd.fotos_modelo); ilustraciones en _figuras.html
  src/idcmd_central/demo.py      cliente demo (equipos simulados, historial; nunca avisa: es_demo)
docs/        adr/, runbooks/, plantillas/, supuestos.md
despliegue/  laboratorio/ (compose + iniciar.ps1 para un PC), minipc/ (nftables),
             oci/ (Cloud Shell: red, servidor, IP, bucket), produccion/ (Caddy HTTPS, instalar.sh,
             respaldos GPG cada 6 h, prueba de restauración), sitio/ (agente -> central, conectar-sitio.ps1)
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
IDCMD_DSN_ADMIN=... IDCMD_LLAVE_TOTP=... uv run idcmd-central usuarios crear --correo ... --rol admin
IDCMD_DSN=... IDCMD_LLAVE_TOTP=... uv run idcmd-central worker      # tickets, SLA y avisos
docker build -f central/Dockerfile -t idcmd-central .   # desde la raíz del repositorio
```
Laboratorio completo en un PC: `docs/runbooks/laboratorio.md`.

## Base de datos central (reglas)

- Roles: `idcmd_app` (portal) e `idcmd_ingesta` (agentes), sin login, sin BYPASSRLS, no
  dueños de tablas. El usuario de login es miembro NOINHERIT: sin `SET LOCAL ROLE` no tiene
  permisos. La aplicación **nunca** se conecta con el rol dueño.
- **Toda tabla nueva lleva RLS** (`test_todas_las_tablas_tienen_rls` lo exige) y, si tiene
  datos de un sitio, `cliente_id` + `sitio_id` con llave foránea compuesta.
- Visibilidad: `idcmd.sitios_visibles()` (admin todo; técnico sus asignaciones vigentes;
  cliente su alcance cliente/regional/sitio). Escritura de inventario: solo Admin.
- Hashes de tokens y claves del portal: el rol `idcmd_app` puede escribirlos, nunca leerlos.
  La clave del portal se verifica DENTRO de Postgres (`idcmd.verificar_clave`, bcrypt); 5 fallos
  (clave o código TOTP) bloquean 15 min y el contador solo se reinicia con clave Y código
  correctos. Sesiones: la cookie lleva un token; la base guarda su SHA-256 (`idcmd.sesiones`,
  solo por funciones).
- `auditoria_accesos` solo admite INSERT.
- Tickets: el portal solo los lee; toda acción pasa por `idcmd.accion_ticket` (valida quién y qué
  transición). `eventos_ticket` solo se agrega; `visible_cliente = false` para notas y eventos
  internos. El worker usa `idcmd_worker` (políticas explícitas, sin BYPASSRLS).
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

## Adaptadores disponibles

| Adaptador | Equipos | Lee | Estado |
|---|---|---|---|
| `icmp` | Cualquiera con IP | Respuesta, latencia, pérdida | Probado en contenedor |
| `hikvision_acceso` | Paneles/terminales Hikvision (ISAPI) | Modelo, firmware, serie, reloj, zona horaria, NTP, tamper, lectoras, batería | ✅ Validado en oficina: DS-K2602T, fw V1.1.1 (2023-09-16), http |
| `dahua_acceso` / `dahua_camara` | Controladoras y cámaras Dahua (CGI) | Modelo, firmware, serie, reloj | Validar con equipo real |
| `dahua_nvr` | NVR Dahua (de sistema, con `canales`) | Identidad, discos, uso, cámara conectada por canal | ✅ Validado en oficina con `idcmd_monitor`: DHI-NVR5432-EI, fw 5.001 (2026-03-31), http |
| `dahua_registro` / `hikvision_registro` | Registro de operaciones (ADR 0009) | Quién entró (cuenta, IP) y qué hizo; descarta eventos (IVS, audio, alarmas) | Dahua ✅ oficina (NVR5432, fw 5.001): IP en `Detail`, login en 2 entradas, textos en español. Hikvision ✅ panel DS-K2602T vía `AcsEvent` major 3 (no tiene logSearch ni anota cambios de configuración: esos los cubren las instantáneas) |
| `dahua_retencion` | NVR Dahua (`deteccion: false`, cada 15 min) | Retención real y proyectada por cámara vs contratada; grabación reciente por canal | ✅ Validado en oficina: 120,3 reales / 226,6 proyectados; el filtro por canal funciona (respuesta numerada desde 0) |
| `simulado` | Demos | Caídas ficticias reproducibles | — |

Para validar uno contra un equipo real: `idcmd sondear --adaptador <nombre> --direccion <ip>
--usuario <u> [--param clave=valor] [--capturar ./dir]`. En laboratorio, mejor guardar la
clave una vez (`idcmd credenciales guardar <ref> --usuario <u> --secretos ./secretos`, pide
confirmarla) y usar `--credencial <ref> --secretos ./secretos`: escribir la clave a ciegas en
cada prueba bloqueó cuentas del NVR de la oficina. Nunca poner claves en scripts ni en el chat. Las capturas, revisadas y sin
datos reales, reemplazan las respuestas armadas en `agente/tests/fixtures/`.

Instantáneas: además de los eventos (cambio de estado), el agente envía las métricas
vigentes cuando cambia una métrica no volátil y al menos cada hora (`nucleo/instantaneas.py`).
La central guarda el valor actual y el historial de cambios (`cambios_metricas`): esa es la
auditoría de configuración de los equipos. `METRICAS_VOLATILES` es igual en agente y central.

Adaptadores "de sistema" (un NVR que informa por sus cámaras): el objetivo declara
`canales: {canal: dispositivo_id}` y el planificador acepta resultados solo para esos
dispositivos. Chequeos lentos que no miden disponibilidad usan `deteccion: false`.
Dahua: **nunca** usar `configManager.cgi` (RemoteDevice devuelve las claves de las cámaras).

## Cómo agregar una marca (criterio: sin tocar el núcleo)

1. Crear `agente/src/idcmd_agente/adaptadores/<marca>.py` con una clase
   `@registrar("<marca>")` que implemente `chequear(objetivo, credencial)`.
2. Usar `ClienteSoloLectura` con la lista blanca mínima de `Operacion(metodo, ruta, parametros)`.
   La lista cubre también los parámetros de la consulta: en APIs tipo CGI (Dahua) la misma
   ruta lee o actúa según `action` (`reboot`, `openDoor`). Nunca escribir la consulta dentro
   de la ruta; pasarla en `params`.
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
- `docs/plantillas/base_conocimiento.csv` se copia a `central/src/idcmd_central/servicio/`;
  editar la de `docs/` y volver a copiar (una prueba exige que sean idénticas).

## Estado

| Etapa | Contenido | Estado |
|---|---|---|
| E1 | Núcleo del agente (este código), contrato v1 | ✅ Entrega 2 |
| E1 | Central: esquema Postgres + RLS, API de ingesta | ✅ Entrega 3 |
| E2 | Adaptadores Hikvision acceso, Dahua acceso/cámara/NVR/retención + `idcmd sondear` | ✅ Entrega 3b (validar en oficina) |
| E1 | Portal mínimo con login MFA y estado en vivo + modo laboratorio | ✅ Entrega 4 (probado de punta a punta en contenedores; falta en la oficina) |
| E3 | Servicio parte 1: tickets automáticos, asignación/escalamiento, SLA, avisos, seguimiento | ✅ Entrega 5 (probado de punta a punta en contenedores) |
| E3 | Servicio parte 2: PWA del técnico (llegada con ubicación, pasos, fotos, firma, acta) | ✅ Entrega 6 (probado en celular emulado con GPS) |
| E5 | Producción: central en OCI Bogotá con HTTPS, respaldos cifrados, agente de sitio | ✅ Entrega 7 (publicada en command.idtech.com.co) |
| E4 | Informe mensual de SLA: disponibilidad ponderada, exclusiones, cumplimiento de tickets, tendencia, PDF, envío el día 1 | ✅ Entrega 8 (probado con Postgres real y PDF en el contenedor) |
| E4 | Reporte de servicios (por equipo, falla, técnico; CSV) y cliente demo | ✅ Entrega 9 |
| E4 | Administración en el portal (sitios, técnicos, asignaciones, QR del 2FA) y gestión (rendimiento de técnicos, confiabilidad de equipos) | ✅ Entrega 10 |
| E4 | Apariencia de los equipos en el tablero (figura por referencia, foto por modelo) | ✅ Entrega 11 |
| E3–E6 | Servicio parte 3 (acta PDF, encuesta, cotización de repuestos), piloto | Pendiente |

# Supuestos aprobados (2026-10-26)

Pablo aprobó todos los supuestos ("ok"). Si alguno cambia, actualizar aquí y en el ADR afectado.

| # | Supuesto | Verificar en |
|---|---|---|
| P1/P6 | Central en OCI Bogotá con Postgres y autenticación propios; IDtech opera la base. | ADR 0002 |
| P2 | Milestone XProtect 2023 R1+ con API Gateway; BioStar 2.8+. Retención real por cámara se confirma con un spike (no hay endpoint REST documentado de grabación más antigua). | Etapa 0 |
| P3 | SLA 24×7; excluye mantenimiento y causas atribuibles al cliente; el tiempo sin datos se informa aparte y no cuenta como caída. Todo parametrizable por contrato. | ADR 0004, contrato real |
| P4 | La entidad final puede tener usuario de solo lectura limitado a sus sitios (RLS por alcance cliente/regional/sitio). | Entrega 3 |
| P5 | El agente soporta `HTTPS_PROXY` y CA corporativa desde el día 1. | Hecho (Entrega 2) |
| P7 | Informe al cliente por enlace al portal; adjunto PDF configurable por cliente. | ADR 0003 |
| P8 | PWA en línea con tolerancia (borradores, reintento de fotos); sin señal = Fase 2. | ADR 0003 |
| P9 | 30 min de verificación antes del cierre, configurable por contrato. | ADR 0004 |
| P10 | Dahua no entra al MVP salvo que el sitio piloto tenga muchos equipos Dahua. | Datos del sitio |

## Cambios posteriores (2026-10-26)

- **Sitio piloto = oficina de IDtech** (Pablo es el cliente). No requiere autorización de
  terceros; se deja un acta interna como plantilla para clientes. Valida la tecnología,
  no la disposición a pagar de un cliente externo.
- **Oficina:** panel de acceso Hikvision **DS-K2602T** (2 puertas, ISAPI, http puerto 80;
  inicialmente se creyó DS-K2622X, SADP mostró el modelo real) y
  cámaras + NVR Dahua. La retención real por cámara se valida con el NVR Dahua.
- **Primeros adaptadores nativos: control de acceso Hikvision (ISAPI) y Dahua (CGI).**
  Milestone y BioStar 2 pasan al primer cliente externo. Reemplaza P10 (Dahua sí entra).
- Modo laboratorio: central, base y agente en un mismo equipo de la oficina; Oracle Cloud
  Bogotá cuando el piloto esté estable.

## Validación en la oficina (2026-10-27)

- NVR Dahua DHI-NVR5432-EI (32 canales), firmware 5.001.0000000.2.R build 2026-03-31, por
  http: identidad, desfase de reloj (2,3 s), discos (sin error, 52,7 % de uso) y estado por
  canal (getCameraState) responden como se esperaba.
- Retención real: 119,9 días en los canales 1, 2 y 5. El disco no está lleno, así que hoy la
  retención es la antigüedad de la grabación, no un límite. Pendiente: confirmar con la
  captura que la búsqueda respeta el canal (los tres dieron el mismo valor).
- Canales 3 y 4 sin cámara: el NVR informa los canales vacíos igual que una cámara
  desconectada. Regla: solo se vigilan los canales declarados en el inventario (`canales`);
  el inventario de cada sitio debe listar exactamente los canales con cámara.

- Panel DS-K2602T (fw V1.1.1 build 20230916) en 192.168.1.200: el adaptador leyó todo y
  encontró y Pablo confirmó: **caja abierta** (tapa abierta), **sin batería de respaldo**
  (0 V) y **zona horaria UTC+0 sin NTP** (la hora de pared es correcta: −1,9 s). Se separó
  `zona_horaria_incorrecta` de `reloj_desfasado`.
- `cardReaderOnlineStatus: [1, 3]` en este firmware es la lista de DIRECCIONES RS-485 de las
  lectoras en línea (dos lectoras, direcciones 1 y 3), no un estado por puerto. La primera
  versión lo leyó mal y dio una falsa "lectora desconectada"; corregido y cubierto con
  pruebas. Configurar `lectoras_direcciones: "1,3"` para saber cuál falta si una cae.

- Pedidos de Pablo (2026-10-27), implementados: totales de usuarios/tarjetas y modo de
  verificación por lectora (ADR 0008); alertas de almacenamiento del NVR (capacidad
  reducida, disco casi lleno solo si el NVR no sobrescribe) y retención PROYECTADA: un NVR
  al 100 % sobrescribiendo es lo normal; un NVR nuevo con pocos días reales no es falla.
- Auditoría de accesos administrativos (ADR 0009, opción A aprobada): cuenta e IP de origen.
  Pendiente: validar los formatos de log.cgi y logSearch con los equipos de la oficina, y
  crear la cuenta `idcmd_monitor` en cada equipo. Pendiente legal: incluirlo en la política
  de tratamiento de datos y en los contratos.

- **Bloqueo de cuenta en el NVR (2026-10-27).** Tras muchas pruebas con `admin`, el NVR
  respondió 403 `{"ErrorCode": 268632081, "RmLock": 1514, "RmLogin": 0}` a todo: cuenta
  bloqueada ~25 min. Riesgo de producción: un agente con clave vieja reintentando cada minuto
  dejaría bloqueada la cuenta indefinidamente. Corregido: detección `cuenta_bloqueada`,
  pausa por clave rechazada y cuenta propia del agente obligatoria.
- Modos de verificación (DS-K2602T): las lectoras 1 y 3 tienen el mismo modo (iVMS:
  huella o tarjeta) pero el panel reporta [7, 3, 3, 3]; se envía la lista cruda. Código 7
  confirmado. Pendiente: leer el plan de autenticación por lectora para el modo exacto.

- Retención proyectada validada con el NVR real (2026-10-27, versión 9): 120,2 días reales y
  227,1 proyectados en los canales 1, 2 y 5 frente a 30 contratados. La "falla" previa era
  el bloqueo de la cuenta, no la búsqueda. `cuenta_bloqueada` (1800 s) también se vio en
  real con `idcmd_monitor`, tras varios intentos con clave rechazada por la API.

- Registro de operaciones del NVR (`dahua_registro`, 2026-10-27): log.cgi responde, pero
  ~200 entradas por hora son EVENTOS de analítica ("IVS", "GlobalSence IVS", "Detección de
  audio") anotados a nombre de `admin`, con los textos en español. Con el tope de 500 la
  lectura de 24 h cubría solo 2,5 h y saltaba el resto (se perdían los inicios de sesión
  del día). Corregido: se descartan eventos, se clasifica en español y, con el tope
  alcanzado, se sigue desde la última entrada.
- Registro con `idcmd_monitor`, 6 h (2026-10-27 21:24Z): log.cgi responde con la cuenta del
  agente (la clave es correcta). 1599 eventos descartados (1056 "Detección de audio", 543
  "IVS"). Textos reales de operación: "Usuario conectado.", "Cierre ssión usua" (truncado
  por el equipo), "Añadir Usuario", "Illegal Login", "Fallo al iniciar sesión.". Ya se
  clasifican.
- Formato real del registro (NVR5432, fw 5.001): `User` es la cuenta (todas eran `admin`
  porque fue Pablo); la IP no tiene campo propio y va en `Detail` ("Dirección IP de inicio
  de sesión:x.x.x.x"); cada inicio de sesión ocupa dos entradas (grupo e IP, ~6 s) y cada
  fallo otras dos ("Illegal Login" y "Fallo al iniciar sesión.", sin IP). Se unen de a
  pares. "Añadir Usuario" a las 14:09 fue la recreación de `idcmd_monitor`: desde entonces
  la cuenta funciona también en `dahua_nvr` (21:31Z, ok).
- Canal 5 "caído" (2026-10-27): real. Pablo movió la cámara al canal 3; el NVR reporta el
  canal 5 como `"Empty"` (no `"Disconnected"`). Ahora se informa `canal_sin_camara`
  (causa de configuración) y no `camara_desconectada_nvr`. Pendiente: qué responde el NVR
  cuando a la cuenta le falta permiso sobre un canal.
- Retención por canal (2026-10-27): el filtro sí funciona (canal 3 pedido -> `Channel=2`
  en la respuesta), pero los tres canales daban 120,3 días porque el canal 3 conserva
  archivos de junio de una cámara anterior. La más antigua no dice si la cámara sigue
  grabando: se agregó la búsqueda de grabación en las últimas 2 h (`sin_grabacion_reciente`)
  y la verificación del canal de la respuesta (`busqueda_canal_inconsistente`).

## Pendientes de Pablo (ruta crítica, no son código)
- Inventario de la oficina: modelo, firmware e IP de cada panel Hikvision y Dahua.
- Usuario de solo lectura (u operador) en cada panel para el agente.
- Equipo anfitrión del laboratorio (mini PC Linux o PC Windows con Docker Desktop).
- Cuenta OCI con Bogotá como *home region* (no se puede cambiar después).
- Mini PC: Intel N100 o similar, 16 GB RAM, 512 GB SSD, TPM 2.0, idealmente 2 puertos de red.
- Reglas de SLA del contrato: tiempos de respuesta/solución por prioridad, horario.
- Contenido inicial de la base de conocimiento (`docs/plantillas/base_conocimiento.csv`).

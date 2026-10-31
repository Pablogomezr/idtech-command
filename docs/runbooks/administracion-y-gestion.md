# Administración y reportes de gestión

Todo desde el portal, sin consola. Solo el **administrador** ve el menú *Administración*.

## Crear un sitio
*Administración → + Nuevo sitio*.
- Cliente: elegir uno existente o escribir el nombre de uno nuevo (se crea solo).
- Regional (opcional, p. ej. "Atlántico"): también se crea si no existe.
- Nombre, ciudad, latitud y longitud (acepta coma o punto decimal).
- Técnico principal y suplente: el worker asigna los tickets del sitio al principal y escala al
  suplente si no responde.

Los **equipos** del sitio no se crean aquí: llegan con el inventario del agente
(`conectar-sitio.md`), que es el que sabe sus direcciones y credenciales.

Al editar un sitio no se puede cambiar el cliente (los datos históricos quedan con el suyo).

## Crear un técnico o un supervisor del contrato
*Administración → + Nuevo usuario*.
- **Técnico**: marcar los sitios que atiende. Queda como técnico principal; con la casilla
  *Técnico suplente* queda como suplente. Sin sitio no se crea (entraría y no vería nada).
- **Cliente** (supervisor del contrato): elegir su cliente para que vea todos sus sitios, o
  marcar sitios puntuales. Solo lectura.
- **Administrador**: ve todo.

Al crear, el portal muestra **una sola vez** la clave del portal y el código QR del segundo
factor. La persona escanea el QR con Google Authenticator (o Microsoft Authenticator) en su
celular y anota la clave. Entréguelo en persona o en una llamada: **nunca** por correo ni
WhatsApp, ni en capturas de pantalla.

Acciones por usuario:
- **Acceso**: agregar o quitar sitios / cliente.
- **Nueva clave**: genera clave y segundo factor nuevos y cierra sus sesiones (celular perdido,
  clave olvidada, persona bloqueada).
- **Desactivar**: no puede entrar más y se cierran sus sesiones. Nadie puede desactivarse a sí
  mismo. Se puede volver a activar.

Todo queda en la auditoría (`admin_sitio`, `admin_usuario_*`).

## Reportes de gestión (*Servicios*)
Tres pestañas, con periodo (desde/hasta, por mes) y sitio. Cada una descarga Excel (CSV con `;`).

| Pestaña | Quién la ve | Qué responde |
|---|---|---|
| Resumen | todos | cuántos servicios, por equipo, falla, sitio o técnico; cumplimiento de SLA |
| Técnicos | admin (todos); técnico ("Mi rendimiento", solo él) | tiempo de respuesta, llegada y solución; % a tiempo; reaperturas; retrabajos; % resuelto a la primera |
| Equipos | todos, en sus sitios | disponibilidad, caídas, cada cuánto falla, cuánto dura la caída, servicios, visitas, reincidencias |

El cliente no ve la pestaña Técnicos: los nombres y el rendimiento del personal son de IDtech.

### Definiciones (fijadas en `central/tests/test_gestion.py`)
- **Respuesta**: de la apertura del ticket (la falla) a que el técnico acepta.
- **Llegada**: de la apertura a que confirma en el sitio (con ubicación).
- **Solución**: de la apertura a que lo marca reparado.
- **Reapertura**: lo marcó resuelto, pero la reverificación vio el equipo aún en falla.
- **Retrabajo**: después de un cierre verificado, el **mismo equipo** vuelve a fallar con la
  **misma falla** dentro de 30 días. Cuenta contra quien cerró el primero.
- **Resuelto a la primera**: reparados sin reapertura ni retrabajo ÷ reparados.
- **Reincidencia de un equipo**: un servicio que se abre dentro de 30 días después de cerrado el
  anterior del mismo equipo (cualquier falla). Varias reincidencias = candidato a cambio de equipo
  o a revisar la instalación (energía, PoE, cableado): argumento para una cotización.
- **Falla cada** (MTBF): tiempo medido ÷ número de caídas. **Duración media** (MTTR): tiempo
  caído ÷ caídas. Mismas reglas de medición del informe de SLA (ADR 0004).
- Los tickets que se recuperaron solos no cuentan en los porcentajes de a tiempo.

Para el comité del contrato: Equipos + Resumen del mes, descargados en Excel, junto con el
informe mensual de SLA (PDF).

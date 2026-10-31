# Cliente demo y reporte de servicios

## Cliente demo (para mostrar a prospectos)
"Cliente Demo (datos simulados)" tiene una "Sede Demo" con 7 equipos simulados: un NVR, 5 cámaras
y un panel de acceso.

- **En vivo:** los equipos corren en vivo en el mismo servidor, con caídas ficticias de vez en
  cuando. Así se ven tickets que se abren y se cierran solos.
- **Historial:** trae unos 2 meses de caídas, servicios cerrados e informes mensuales.
- **Sin avisos:** **nunca envía avisos** por correo ni Telegram (`clientes.es_demo`).
- **Separado:** no toca ni mezcla los datos reales.

Crear la demo (una vez; repetirla no duplica nada). En el servidor:

```bash
sudo idcmd-central demo preparar --correo demo@idtech.com.co --correo-tecnico tecnico.demo@idtech.com.co
sudo idcmd-demo-iniciar          # arranca los equipos simulados (y queda arrancando con el servidor)
```

El comando muestra, una sola vez, la **clave generada** y el **segundo factor** de dos usuarios:

| Usuario | Para qué |
|---|---|
| `demo@idtech.com.co` (cliente) | Ve solo la Sede Demo: estado en vivo, servicios, informes. |
| `tecnico.demo@idtech.com.co` (técnico) | La app del técnico con los tickets de la demo. |

Guarda las claves en tu gestor y agrega los dos segundos factores a tu app de autenticación. En
una presentación entras tú con el usuario demo desde el celular o el PC del cliente.

Sin ingresar, también puedes mostrar el **enlace del informe** de la Sede Demo: en Portal →
Informes, con tu usuario admin, usa "Enlace para el cliente".

Si se pierde el celular o la clave:
`sudo idcmd-central usuarios restablecer-mfa --correo demo@idtech.com.co --generar-clave`.

## Reporte de servicios (Portal → Servicios)
Muestra cuántos servicios (tickets) hubo en un rango de meses, agrupados por:
- **equipo**;
- **tipo de falla**;
- **técnico**;
- **prioridad**;
- **sitio**.

Para cada grupo indica cuántos se repararon, cuántos se recuperaron solos y cuántos siguen en
curso. También muestra el % de respuesta y de solución a tiempo y el tiempo medio de solución.

- **Excel:** "Descargar Excel (CSV)" baja el detalle completo, con todas las horas (abierto,
  aceptado, en sitio, resuelto, cerrado) y si se cumplió el SLA. Abre directo en Excel en español.
- **Quién ve qué:**
  - el **cliente** ve sus sitios, sin nombres de técnicos;
  - el **técnico** ve por defecto sus servicios, con la casilla para ver los de otros técnicos;
  - el **admin** ve todo.

## Claves sin teclear
Si al escribir una clave en la consola sale "las claves no coinciden" por teclas especiales (p. ej.
una F7 trabada mete `^[[18~`), usa `--generar-clave` en `usuarios crear` o en
`usuarios restablecer-mfa`: la consola genera la clave y la muestra para copiarla.

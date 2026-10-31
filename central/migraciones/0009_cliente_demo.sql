-- Cliente de demostración: datos simulados para mostrar el portal a prospectos. Sus fallas abren
-- tickets como cualquier otra (así se ve el flujo), pero nunca generan avisos por correo ni
-- Telegram: nadie debe recibir alertas de equipos que no existen.
ALTER TABLE idcmd.clientes ADD COLUMN es_demo boolean NOT NULL DEFAULT false;

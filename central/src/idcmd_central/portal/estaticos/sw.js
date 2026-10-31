/* Service worker del portal: muestra "sin conexión" cuando no hay señal y guarda lo estático
   SOLO como respaldo sin señal. Primero la red: tras una actualización se ven de inmediato los
   estilos nuevos (con "primero la caché" el portal quedó con la hoja vieja). Nunca guarda
   páginas con datos (cada usuario ve solo lo suyo). La central reemplaza __VERSION__. */
const CACHE = "idcmd-__VERSION__";
const ESTATICOS = ["/portal/sin-conexion", "/portal/estaticos/icono-192.png"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ESTATICOS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/portal/estaticos/")) {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copia)); }
      return r;
    }).catch(() => caches.match(e.request)));
  } else if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/portal/sin-conexion")));
  }
});

/* Service worker del portal: guarda lo estático para abrir rápido y muestra "sin conexión"
   cuando no hay señal. Nunca guarda páginas con datos (cada usuario ve solo lo suyo). */
const CACHE = "idcmd-v1";
const ESTATICOS = [
  "/portal/estaticos/portal.css", "/portal/estaticos/htmx.min.js", "/portal/estaticos/campo.js",
  "/portal/estaticos/icono-192.png", "/portal/sin-conexion",
];
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
    e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
  } else if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/portal/sin-conexion")));
  }
});

/* IDtech Command — app del técnico: ubicación al llegar, fotos reducidas en el celular, firma
   con el dedo, impresión del acta y registro de la PWA. Sin dependencias. */
(function () {
  "use strict";

  // PWA: instalable y con página "sin conexión".
  if ("serviceWorker" in navigator && location.pathname.indexOf("/portal") === 0) {
    navigator.serviceWorker.register("/portal/sw.js", { scope: "/portal/" }).catch(function () {});
  }

  function bloquear(form, texto) {
    var b = form.querySelector("button[type=submit]");
    if (b) { b.disabled = true; b.dataset.texto = b.textContent; b.textContent = texto; }
  }

  // "Llegué al sitio": la ubicación se pide SOLO aquí, una vez. Si el técnico la niega o no hay
  // señal de GPS, se registra la llegada igual (queda constancia de que no la compartió).
  document.querySelectorAll("form[data-geolocalizar]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (form.dataset.listo) return;
      ev.preventDefault();
      bloquear(form, "Ubicando…");
      var enviar = function () { form.dataset.listo = "1"; form.submit(); };
      if (!("geolocation" in navigator)) return enviar();
      navigator.geolocation.getCurrentPosition(function (pos) {
        form.lat.value = pos.coords.latitude.toFixed(6);
        form.lon.value = pos.coords.longitude.toFixed(6);
        form.precision.value = Math.round(pos.coords.accuracy);
        enviar();
      }, enviar, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
    });
  });

  // Fotos: se reducen a 1600 px y JPEG en el celular (sube en segundos con mala señal y, al
  // redibujarla, la foto pierde los metadatos EXIF, incluida la ubicación GPS).
  function reducir(archivo, lado, calidad) {
    return new Promise(function (ok, mal) {
      var img = new Image();
      img.onload = function () {
        var escala = Math.min(1, lado / Math.max(img.width, img.height));
        var c = document.createElement("canvas");
        c.width = Math.round(img.width * escala); c.height = Math.round(img.height * escala);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(img.src);
        c.toBlob(function (b) { b ? ok(b) : mal(new Error("no se pudo reducir")); }, "image/jpeg", calidad);
      };
      img.onerror = mal;
      img.src = URL.createObjectURL(archivo);
    });
  }

  function enviarPorFetch(form, datos) {
    return fetch(form.action, { method: "POST", body: datos, credentials: "same-origin", redirect: "follow" })
      .then(function (r) { location.href = r.url || location.href; });
  }

  document.querySelectorAll("form[data-comprimir]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      var archivo = form.foto && form.foto.files[0];
      if (!archivo || !window.fetch || !HTMLCanvasElement.prototype.toBlob) return; // envío normal
      ev.preventDefault();
      bloquear(form, "Subiendo…");
      reducir(archivo, 1600, 0.82).then(function (blob) {
        var datos = new FormData(form);
        datos.set("foto", blob, "foto.jpg");
        return enviarPorFetch(form, datos);
      }).catch(function () { form.submit(); });
    });
  });

  // Firma con el dedo o el mouse.
  document.querySelectorAll("form[data-firma]").forEach(function (form) {
    var lienzo = form.querySelector("canvas"), ctx = lienzo.getContext("2d"), trazando = false, trazos = 0;
    ctx.lineWidth = 3; ctx.lineCap = "round"; ctx.strokeStyle = "#111";
    function punto(e) {
      var r = lienzo.getBoundingClientRect();
      return { x: (e.clientX - r.left) * lienzo.width / r.width, y: (e.clientY - r.top) * lienzo.height / r.height };
    }
    lienzo.addEventListener("pointerdown", function (e) {
      trazando = true; trazos++; lienzo.setPointerCapture(e.pointerId);
      var p = punto(e); ctx.beginPath(); ctx.moveTo(p.x, p.y);
    });
    lienzo.addEventListener("pointermove", function (e) {
      if (!trazando) return; var p = punto(e); ctx.lineTo(p.x, p.y); ctx.stroke();
    });
    ["pointerup", "pointercancel"].forEach(function (t) { lienzo.addEventListener(t, function () { trazando = false; }); });
    form.querySelector("[data-borrar]").addEventListener("click", function () {
      ctx.clearRect(0, 0, lienzo.width, lienzo.height); trazos = 0;
    });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!trazos) { alert("Falta la firma."); return; }
      bloquear(form, "Guardando…");
      // Fondo blanco: la firma se ve igual en el acta impresa y en modo oscuro.
      var c = document.createElement("canvas"); c.width = lienzo.width; c.height = lienzo.height;
      var x = c.getContext("2d"); x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height); x.drawImage(lienzo, 0, 0);
      c.toBlob(function (blob) {
        var datos = new FormData(form); datos.set("firma", blob, "firma.png");
        enviarPorFetch(form, datos);
      }, "image/png");
    });
  });

  // Confirmación antes de acciones de administración (sin JavaScript en línea: lo prohíbe la CSP).
  document.querySelectorAll("form[data-confirmar]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirmar"))) e.preventDefault();
    });
  });
  document.querySelectorAll("[data-imprimir]").forEach(function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); window.print(); });
  });
})();

"""Panel web para configurar el área de análisis (ROI).

Permite:
  1. Ver el video en vivo de la cámara.
  2. Dibujar un rectángulo sobre la zona que interesa (ej: el display).
  3. Guardar el área → el detector solo analiza esa región.

El área se guarda en el archivo `roi.json` y el detector la recarga
en cada ciclo, así los cambios se aplican sin reiniciar el backend.
"""

import json
import threading
from datetime import datetime
from pathlib import Path

import cv2
from flask import Flask, Response, jsonify, request, send_file

# Rutas ABSOLUTAS basadas en el directorio del proyecto (no en el cwd),
# para que funcionen sin importar desde dónde se ejecute el proceso.
RAIZ = Path(__file__).resolve().parent.parent
RUTA_ROI = RAIZ / "roi.json"
RUTA_CAPTURAS = RAIZ / "capturas_cambio"

# Notificador de capturas nuevas: el registrador incrementa el contador
# y el panel web avisa al navegador (SSE) solo cuando hay algo nuevo.
_contador_capturas = 0
_condicion_capturas = threading.Condition()


def notificar_captura_nueva():
    """Llama el registrador cuando guarda una imagen nueva."""
    global _contador_capturas
    with _condicion_capturas:
        _contador_capturas += 1
        _condicion_capturas.notify_all()


def cargar_roi():
    """Devuelve [left, top, width, height] o None si no hay área definida."""
    if RUTA_ROI.exists():
        try:
            datos = json.loads(RUTA_ROI.read_text(encoding="utf-8"))
            if all(k in datos for k in ("left", "top", "width", "height")):
                return [datos["left"], datos["top"],
                        datos["width"], datos["height"]]
        except (json.JSONDecodeError, OSError):
            pass
    return None


def guardar_roi(region):
    RUTA_ROI.write_text(
        json.dumps({
            "left": region[0],
            "top": region[1],
            "width": region[2],
            "height": region[3],
        }),
        encoding="utf-8",
    )


def _parsear_fecha(nombre_archivo: str):
    """Extrae la fecha del nombre del archivo: evento_YYYYMMDD_HHMMSS_fff_..."""
    try:
        parte = nombre_archivo.replace("evento_", "").split("_")[0:2]
        fecha = datetime.strptime("_".join(parte), "%Y%m%d_%H%M%S")
        return fecha
    except (ValueError, IndexError):
        return None


def ultimas_capturas(cantidad=2):
    """
    Devuelve las últimas `cantidad` imágenes originales capturadas
    (excluye las versiones marcadas). Ordenadas de más reciente a más antigua.
    """
    if not RUTA_CAPTURAS.exists():
        return []
    archivos = sorted(
        RUTA_CAPTURAS.glob("evento_*_original.png"),
        key=lambda p: p.name,
        reverse=True,  # más reciente primero (el nombre tiene el timestamp)
    )
    resultado = []
    for archivo in archivos[:cantidad]:
        fecha = _parsear_fecha(archivo.name)
        resultado.append({
            "archivo": archivo.name,
            "url": f"/capturas/{archivo.name}",
            "fecha": fecha.strftime("%d/%m/%Y %H:%M:%S") if fecha else "",
        })
    return resultado


def leer_log_eventos(archivo_log: Path, cantidad=20):
    """Lee las últimas `cantidad` líneas del log JSONL de eventos."""
    if not archivo_log.exists():
        return []
    eventos = []
    try:
        with open(archivo_log, encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    eventos.append(json.loads(linea))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return eventos[-cantidad:][::-1]  # más recientes primero


def sugerencia_ajuste(evento: dict, min_area_px: int) -> str:
    """
    Devuelve una recomendación de qué parámetro ajustar según el evento.
    """
    area = int(evento.get("area_px", 0))

    if area <= 0:
        return "Sin datos de área."

    # Qué tan cerca está del umbral actual
    margen = area / min_area_px if min_area_px > 0 else 0

    if margen < 2:
        return (
            f"El cambio tocó solo {area} px (umbral {min_area_px}). "
            f"Está MUY al límite → si son falsos positivos, sube "
            f"min_area_px a {min_area_px * 3} en config.yaml"
        )
    elif margen < 5:
        return (
            f"Cambio de {area} px ({margen:.1f}x el umbral). "
            f"Filtro intermedio → sube min_area_px a {int(area * 1.5)} "
            f"si quieres ignorar cambios así de pequeños"
        )
    else:
        return (
            f"Cambio grande ({area} px, {margen:.1f}x el umbral). "
            f"Dificilmente es ruido — es un cambio real del panel"
        )


def construir_log_web(archivo_log: Path, min_area_px: int):
    """Devuelve la lista de eventos con su sugerencia de ajuste, para la web."""
    resultado = []
    for ev in leer_log_eventos(archivo_log, cantidad=30):
        resultado.append({
            "timestamp": ev.get("timestamp", ""),
            "score": round(float(ev.get("score", 0)), 4),
            "area_px": int(ev.get("area_px", 0)),
            "camara": ev.get("camara", ""),
            "imagen": ev.get("imagen_original", ""),
            "sugerencia": sugerencia_ajuste(ev, min_area_px),
        })
    return resultado


PAGINA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Configurar área de análisis</title>
<style>
  body { font-family: Segoe UI, Arial, sans-serif; background: #1e1e1e;
         color: #eee; margin: 0; padding: 20px; }
  h1 { font-size: 20px; margin: 0 0 12px; }
  p  { font-size: 13px; color: #aaa; margin: 4px 0; }
  #visor { position: relative; display: inline-block; }
  #imagen { display: block; width: 640px; max-width: 100%; border: 2px solid #444;
           border-radius: 6px; }
  #lienzo { position: absolute; inset: 0; cursor: crosshair; }
  .boton { padding: 10px 18px; margin: 8px 8px 0 0; border: 0;
           border-radius: 5px; font-size: 14px; cursor: pointer; }
  #guardar { background: #2e7d32; color: white; }
  #limpiar { background: #c62828; color: white; }
  #estado { margin-top: 12px; font-size: 14px; color: #81c784; }
  .info { max-width: 800px; }
  .columnas { display: flex; gap: 24px; align-items: flex-start;
              flex-wrap: wrap; margin-top: 12px; }
  .col-izq { flex: 0 1 auto; }
  .col-der { flex: 1 1 340px; min-width: 320px; }
  #ultimas { display: flex; flex-direction: column; gap: 16px; }
  .captura { border: 1px solid #444; border-radius: 6px; overflow: hidden;
            background: #262626; }
  .captura img { width: 100%; max-width: 400px; height: 180px;  /* alto fijo:
                 evita saltos de layout al cargar */
                 object-fit: contain; display: block; background: #000; }
  .captura .fecha { padding: 8px 10px; font-size: 13px; color: #ccc; }
  /* Log de eventos con sugerencias */
  #log-caja { margin-top: 16px; }
  .log-encabezado { display: flex; justify-content: space-between;
                    align-items: center; margin-bottom: 8px; }
  #btn-limpiar-log { padding: 6px 12px; border: 0; border-radius: 5px;
                     font-size: 12px; cursor: pointer;
                     background: #c62828; color: white; }
  /* Caja del log: borde visible, fondo propio y scroll independiente */
  #log { max-height: 400px; min-height: 120px; overflow-y: scroll;
         border: 2px solid #444; border-radius: 8px;
         background: #1a1a1a; padding: 10px; }
  #log p { color: #aaa; font-size: 13px; }
  .log-item { border: 1px solid #444; border-radius: 6px; margin-bottom: 8px;
              padding: 8px 12px; background: #262626; font-size: 13px; }
  .log-hora { color: #4fc3f7; font-weight: bold; }
  .log-metricas { color: #eee; margin: 2px 0; }
  .log-sugerencia { color: #ffb74d; margin: 4px 0 0; padding-left: 8px;
                    border-left: 3px solid #ffb74d; }
</style>
</head>
<body>
  <h1>🎯 Definir área de análisis (solo ahí se detectarán cambios)</h1>
  <p class="info">Arrastra el mouse sobre el video para dibujar el rectángulo
     sobre la zona que quieres monitorear (ej: el display del panel).
     El fondo — personas, luces, movimiento — se ignorará fuera del área.</p>
  <div class="columnas">
    <div class="col-izq">
      <div id="visor">
        <img id="imagen" alt="Video de la cámara">
        <canvas id="lienzo"></canvas>
      </div>
      <div>
        <button class="boton" id="guardar">💾 Guardar área</button>
        <button class="boton" id="limpiar">🗑️ Quitar área</button>
      </div>
      <div id="estado"></div>
    </div>

    <div class="col-der">
      <h1 style="margin-top:0">📸 Últimas capturas</h1>
      <div id="ultimas">
        <p>Cargando...</p>
      </div>

      <div id="log-caja">
        <div class="log-encabezado">
          <h1 style="margin:0">📋 Log de cambios detectados</h1>
          <button class="boton" id="btn-limpiar-log">🗑️ Limpiar log</button>
        </div>
        <div id="log">
          <p>Cargando...</p>
        </div>
      </div>
    </div>
  </div>

<script>
const imagen = document.getElementById('imagen');
const lienzo = document.getElementById('lienzo');
const ctx = lienzo.getContext('2d');
let dibujando = false, inicioX = 0, inicioY = 0;
let roi = null;

// Área guardada actualmente (si existe)
fetch('/api/roi').then(r => r.json()).then(d => {
  if (d.region) {
    roi = d.region;
    estado('Área actual: ' + roi.join(', ') + ' px');
  } else {
    estado('Sin área definida — se analiza toda la imagen.');
  }
});

function redimensionar() {
  lienzo.width = imagen.clientWidth;
  lienzo.height = imagen.clientHeight;
  dibujar();
}
// El <img> con MJPEG se actualiza solo; se redimensiona al primer frame
imagen.addEventListener('load', redimensionar);
window.addEventListener('resize', redimensionar);

function px_video_x(x) {
  return Math.round(x * imagen.naturalWidth / imagen.clientWidth);
}
function px_video_y(y) {
  return Math.round(y * imagen.naturalHeight / imagen.clientHeight);
}

function dibujar() {
  if (!imagen.clientWidth) return;
  ctx.clearRect(0, 0, lienzo.width, lienzo.height);
  if (!roi) return;
  const x = roi[0] * lienzo.width / imagen.naturalWidth;
  const y = roi[1] * lienzo.height / imagen.naturalHeight;
  const w = roi[2] * lienzo.width / imagen.naturalWidth;
  const h = roi[3] * lienzo.height / imagen.naturalHeight;
  ctx.strokeStyle = '#4fc3f7';
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = 'rgba(79, 195, 247, 0.15)';
  ctx.fillRect(x, y, w, h);
}

lienzo.addEventListener('mousedown', e => {
  dibujando = true;
  const rect = lienzo.getBoundingClientRect();
  inicioX = e.clientX - rect.left;
  inicioY = e.clientY - rect.top;
});

lienzo.addEventListener('mousemove', e => {
  if (!dibujando) return;
  const rect = lienzo.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  roi = [Math.min(inicioX, x), Math.min(inicioY, y),
         Math.abs(x - inicioX), Math.abs(y - inicioY)];
  dibujar();
});

lienzo.addEventListener('mouseup', () => { dibujando = false; });

document.getElementById('guardar').addEventListener('click', async () => {
  if (!roi || roi[2] < 10 || roi[3] < 10) {
    estado('⚠️ Dibuja primero un rectángulo sobre el video.');
    return;
  }
  const region = [px_video_x(roi[0]), px_video_y(roi[1]),
                  px_video_x(roi[2]), px_video_y(roi[3])];
  const res = await fetch('/api/roi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({region}),
  });
  const d = await res.json();
  if (d.ok) {
    estado('✅ Área guardada: ' + region.join(', ') + ' px');
  } else {
    estado('❌ Error al guardar: ' + d.error);
  }
});

document.getElementById('limpiar').addEventListener('click', async () => {
  await fetch('/api/roi', {method: 'DELETE'});
  roi = null;
  dibujar();
  estado('🗑️ Área eliminada — se analiza toda la imagen.');
});

function estado(msg) { document.getElementById('estado').textContent = msg; }

// ── Últimas capturas ────────────────────────────────────────────────
// Actualización INCREMENTAL: solo toca los elementos que cambiaron,
// sin recargar las imágenes existentes ni causar saltos de scroll.
async function actualizarUltimas() {
  let datos;
  try {
    const res = await fetch('/api/ultimas');
    datos = await res.json();
  } catch (e) {
    return; // mantener lo que hay
  }

  const cont = document.getElementById('ultimas');
  const capturas = datos.capturas || [];

  // Estado actual: [archivo1, archivo2] en el DOM
  const actuales = [...cont.querySelectorAll('.captura')]
    .map(el => el.dataset.archivo || '');
  const nuevos = capturas.map(c => c.archivo);

  // Si no hay nada y el DOM está vacío → mensaje
  if (capturas.length === 0) {
    if (actuales.length === 0) {
      cont.innerHTML = '<p>Sin capturas aún — cuando se detecte un cambio, la imagen aparecerá aquí.</p>';
    }
    return;
  }

  // Si la lista es idéntica → no tocar nada (cero saltos de layout)
  if (actuales.length === capturas.length &&
      actuales.every((a, i) => a === nuevos[i])) {
    return;
  }

  // Reconstruir solo cuando realmente cambió la lista
  cont.innerHTML = capturas.map(c => `
    <div class="captura" data-archivo="${c.archivo}">
      <img src="${c.url}?t=${Date.now()}" alt="Captura">
      <div class="fecha">🕐 ${c.fecha}</div>
    </div>`).join('');
}

// ── Log de cambios con sugerencias ──────────────────────────────────
async function actualizarLog() {
  try {
    const res = await fetch('/api/log');
    const d = await res.json();
    const cont = document.getElementById('log');
    if (!d.eventos || d.eventos.length === 0) {
      cont.innerHTML = '<p>Sin eventos aún — cuando se detecte un cambio, aparecerá aquí con su análisis.</p>';
      return;
    }
    cont.innerHTML = d.eventos.map(ev => {
      const hora = ev.timestamp ? ev.timestamp.replace('T', ' ').slice(0, 19) : '';
      return `<div class="log-item">
        <div class="log-hora">🕐 ${hora}</div>
        <div class="log-metricas">
          Cambio: <b>${ev.area_px} píxeles</b> | score: ${ev.score} | umbral actual: ${d.min_area_px}px
        </div>
        <div class="log-sugerencia">💡 ${ev.sugerencia}</div>
      </div>`;
    }).join('');
    // Mantener el scroll ARRIBA: los eventos más recientes están al
    // principio de la lista, así siempre se ven los últimos valores.
    cont.scrollTop = 0;
  } catch (e) {
    document.getElementById('log').innerHTML = '<p>Error al cargar el log.</p>';
  }
}

// Botón para limpiar el log (solo la vista y el archivo en disco)
document.getElementById('btn-limpiar-log').addEventListener('click', async () => {
  try {
    await fetch('/api/log', {method: 'DELETE'});
    actualizarLog();
  } catch (e) {
    alert('Error al limpiar el log');
  }
});

// Cargar al abrir la página
actualizarUltimas();
actualizarLog();

// Actualizar SOLO cuando el servidor avisa que hay una captura nueva
// (sin polling periódico)
const fuenteEventos = new EventSource('/api/eventos');
fuenteEventos.onmessage = () => { actualizarUltimas(); actualizarLog(); };
fuenteEventos.onerror = () => {
  // Si la conexión se corta, EventSource reconecta solo; nada que hacer.
  console.log('Conexión de eventos reconectando...');
};

imagen.src = '/video';
</script>
</body>
</html>
"""


def crear_app(capturador, config=None):
    """Crea la aplicación Flask conectada al capturador activo."""
    global RUTA_CAPTURAS
    if config is not None:
        # Usar la misma carpeta de capturas que el backend
        RUTA_CAPTURAS = Path(config.output_dir)
    app = Flask(__name__)

    # Parámetros del filtro para generar las sugerencias de ajuste
    min_area = config.min_area_px if config is not None else 100
    ruta_log = Path(config.log_eventos) if config is not None \
        else RAIZ / "eventos.jsonl"

    def generar_video():
        """Stream MJPEG en vivo con el ROI dibujado encima."""
        while True:
            try:
                # COPIA del frame: dibujar el ROI sobre la copia, nunca
                # sobre el frame compartido, para no contaminar lo que
                # analiza el detector (causa de falsos positivos).
                frame = capturador.capturar().copy()
                roi = cargar_roi()
                if roi:
                    x, y, w, h = roi
                    cv2.rectangle(frame, (x, y), (x + w, y + h),
                                  (79, 195, 247), 2)
                ok, jpeg = cv2.imencode(".jpg", frame,
                                        [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" +
                           jpeg.tobytes() + b"\r\n")
            except Exception as e:  # noqa: BLE001 — frame fallido, seguir
                # Un frame fallido no debe matar el stream de video
                _ = e
            import time
            time.sleep(0.05)

    @app.route("/")
    def pagina():
        return PAGINA

    @app.route("/video")
    def video():
        return Response(generar_video(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/roi", methods=["GET"])
    def obtener_roi():
        return jsonify({"region": cargar_roi()})

    @app.route("/api/roi", methods=["POST"])
    def definir_roi():
        datos = request.get_json(silent=True) or {}
        region = datos.get("region")
        if not region or len(region) != 4:
            return jsonify({"ok": False, "error": "Se requiere [left, top, width, height]"})
        left, top, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            return jsonify({"ok": False, "error": "Dimensiones inválidas"})
        guardar_roi([left, top, w, h])
        return jsonify({"ok": True})

    @app.route("/api/roi", methods=["DELETE"])
    def eliminar_roi():
        try:
            RUTA_ROI.unlink()
        except OSError:
            pass  # no existía, no hay nada que limpiar
        return jsonify({"ok": True})

    @app.route("/api/ultimas")
    def api_ultimas():
        """Las últimas 2 capturas con su fecha."""
        return jsonify({"capturas": ultimas_capturas(2)})

    @app.route("/api/log")
    def api_log():
        """
        Últimos eventos con su score, área de píxeles y una sugerencia
        de qué parámetro ajustar para el filtro.
        """
        return jsonify({
            "min_area_px": min_area,
            "eventos": construir_log_web(ruta_log, min_area),
        })

    @app.route("/api/log", methods=["DELETE"])
    def limpiar_log():
        """Vacía el archivo de log de eventos (solo lo que está en disco)."""
        try:
            ruta_log.write_text("", encoding="utf-8")
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.route("/api/eventos")
    def api_eventos():
        """
        Server-Sent Events: el navegador mantiene esta conexión abierta
        y recibe un aviso SOLO cuando hay una captura nueva.
        Así las últimas capturas se actualizan al instante, sin polling.
        """
        def generar():
            ultimo = _contador_capturas
            yield ": conectado\n\n"
            while True:
                with _condicion_capturas:
                    _condicion_capturas.wait(timeout=30)
                    cambio = _contador_capturas != ultimo
                    if cambio:
                        ultimo = _contador_capturas
                if cambio:
                    yield f"data: {ultimo}\n\n"

        return Response(generar(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/capturas/<nombre>")
    def servir_captura(nombre):
        """Sirve una imagen capturada (con protección contra rutas fuera de la carpeta)."""
        # Solo nombres de archivos de eventos, sin separadores de ruta
        if "/" in nombre or "\\" in nombre or not nombre.startswith("evento_"):
            return "Archivo no permitido", 400
        ruta = RUTA_CAPTURAS / nombre
        if not ruta.exists():
            return "No encontrado", 404
        return send_file(ruta, mimetype="image/png")

    return app

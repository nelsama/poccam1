"""Panel web para configurar el área de análisis (ROI).

Permite:
  1. Ver el video en vivo de la cámara.
  2. Dibujar un rectángulo sobre la zona que interesa (ej: el display).
  3. Guardar el área → el detector solo analiza esa región.

El área se guarda en el archivo `roi.json` y el detector la recarga
en cada ciclo, así los cambios se aplican sin reiniciar el backend.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("backend")

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

# Notificador de análisis IA nuevos (mismo patrón SSE)
_contador_analisis = 0
_condicion_analisis = threading.Condition()

# Notificador de cambios de CONFIGURACIÓN (para que el panel se refleje
# cuando un cliente externo modifica parámetros por API)
_contador_config = 0
_condicion_config = threading.Condition()


def notificar_config_cambiada():
    """Avisa al panel que la configuración cambió (desde el panel o desde
    un cliente externo por API) para que refresque sus valores."""
    global _contador_config
    with _condicion_config:
        _contador_config += 1
        _condicion_config.notify_all()


def notificar_analisis_nuevo(_ruta=None):
    """Llama el registrador cuando se guarda un análisis de IA nuevo."""
    global _contador_analisis
    with _condicion_analisis:
        _contador_analisis += 1
        _condicion_analisis.notify_all()


def notificar_captura_nueva():
    """Llama el registrador cuando guarda una imagen nueva."""
    global _contador_capturas
    with _condicion_capturas:
        _contador_capturas += 1
        _condicion_capturas.notify_all()


def _bool(v):
    """Convierte a booleano tolerando strings enviados por el navegador."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "si", "yes", "on")
    return bool(v)


def buscar_preset(ancho: int, alto: int) -> dict | None:
    """Devuelve el preset que coincide con la resolución del stream.

    Si no hay coincidencia exacta, devuelve el más cercano por número
    de píxeles (área). None si la resolución es inválida.
    """
    if not ancho or not alto:
        return None
    area = ancho * alto
    for p in PRESETS_CAMARA:
        if p["ancho"] == ancho and p["alto"] == alto:
            return p
    mejor = None
    mejor_diff = None
    for p in PRESETS_CAMARA:
        diff = abs(p["ancho"] * p["alto"] - area)
        if mejor_diff is None or diff < mejor_diff:
            mejor_diff = diff
            mejor = p
    return mejor


# Presets por resolución de cámara (puntos de partida calibrados sobre
# 640x480). El mismo display ocupa más píxeles a mayor resolución:
#   - min_area_px escala con el área (un dígito cambia más píxeles)
#   - max_desplazamiento escala con la dimensión lineal (la misma
#     vibración física se ve más grande)
#   - blur_ksize escala suave con la dimensión lineal
# El resto (metodo, umbral, frames_estables...) es independiente.
PRESETS_CAMARA = [
    {
        "id": "vga", "nombre": "640x480 (VGA)",
        "ancho": 640, "alto": 480,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 50,
                      "blur_ksize": 5, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 10.0},
    },
    {
        "id": "hd720", "nombre": "1280x720 (HD ~1 MP)",
        "ancho": 1280, "alto": 720,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 150,
                      "blur_ksize": 7, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 20.0},
    },
    {
        "id": "fhd", "nombre": "1920x1080 (FullHD ~2 MP)",
        "ancho": 1920, "alto": 1080,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 350,
                      "blur_ksize": 9, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 30.0},
    },
    {
        "id": "mp4", "nombre": "2688x1520 (4 MP)",
        "ancho": 2688, "alto": 1520,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 700,
                      "blur_ksize": 11, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 40.0},
    },
    {
        "id": "mp5", "nombre": "2560x1920 (5 MP)",
        "ancho": 2560, "alto": 1920,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 850,
                      "blur_ksize": 11, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 45.0},
    },
    {
        "id": "k4", "nombre": "3840x2160 (4K ~8 MP)",
        "ancho": 3840, "alto": 2160,
        "deteccion": {"metodo": "ssim", "umbral": 0.5, "min_area_px": 1400,
                      "blur_ksize": 13, "marcar_cambios": False,
                      "frames_estables": 2, "min_intervalo_eventos": 1.0,
                      "alinear_imagenes": False, "max_desplazamiento": 60.0},
    },
]


def _aplicar_config(datos: dict, config, detector, monitor=None):
    """Valida y aplica captura/detección en caliente, y persiste en YAML.

    Acepta todos los parámetros de captura, incluidos `camara_fuente` y
    `fuente`: si cambia la cámara, se recrea el capturador EN VIVO (sin
    reiniciar el proceso) vía `monitor.cambiar_fuente()`. Si la nueva
    cámara no abre, se mantiene la anterior y se lanza RuntimeError.

    Lanza ValueError/TypeError si algún valor es inválido (el llamador
    lo convierte en HTTP 400).
    """
    # ── Captura ──
    c = datos.get("captura") or {}
    if "intervalo_segundos" in c:
        v = float(c["intervalo_segundos"])
        if v <= 0:
            raise ValueError("intervalo_segundos debe ser > 0")
        config.intervalo_segundos = v
    if "rotacion" in c:
        v = int(c["rotacion"]) % 360
        if v not in (0, 90, 180, 270):
            raise ValueError("rotacion debe ser 0, 90, 180 o 270")
        config.rotacion = v
    if "reconectar_segundos" in c:
        v = float(c["reconectar_segundos"])
        if v < 0:
            raise ValueError("reconectar_segundos debe ser >= 0")
        config.reconectar_segundos = v
    if "nombre_camara" in c:
        config.nombre_camara = str(c["nombre_camara"])

    # Cambio de fuente de cámara: se aplica al final (recrea el capturador)
    nueva_fuente = None
    nuevo_tipo = None
    if "fuente" in c:
        tipo = str(c["fuente"])
        if tipo not in ("camara", "pantalla"):
            raise ValueError("fuente debe ser 'camara' o 'pantalla'")
        nuevo_tipo = tipo
    if "camara_fuente" in c:
        nueva_fuente = str(c["camara_fuente"]).strip()
        if not nueva_fuente:
            raise ValueError("camara_fuente no puede estar vacía")

    # ── Detección: validar todo primero, aplicar después ──
    d = datos.get("deteccion") or {}
    nuevos = {}
    if "metodo" in d:
        if d["metodo"] not in ("ssim", "diff", "mse"):
            raise ValueError(f"Método desconocido: {d['metodo']}")
        nuevos["metodo"] = d["metodo"]
    if "umbral" in d:
        nuevos["umbral"] = float(d["umbral"])
    if "min_area_px" in d:
        nuevos["min_area_px"] = max(0, int(d["min_area_px"]))
    if "blur_ksize" in d:
        nuevos["blur_ksize"] = max(0, int(d["blur_ksize"]))
    if "marcar_cambios" in d:
        nuevos["marcar_cambios"] = _bool(d["marcar_cambios"])
    if "frames_estables" in d:
        nuevos["frames_estables"] = max(1, int(d["frames_estables"]))
    if "min_intervalo_eventos" in d:
        v = float(d["min_intervalo_eventos"])
        if v < 0:
            raise ValueError("min_intervalo_eventos debe ser >= 0")
        nuevos["min_intervalo_eventos"] = v
    if "alinear_imagenes" in d:
        nuevos["alinear_imagenes"] = _bool(d["alinear_imagenes"])
    if "max_desplazamiento" in d:
        nuevos["max_desplazamiento"] = max(1.0, float(d["max_desplazamiento"]))

    # Aplicar al detector (en caliente) y a la config compartida
    if detector is not None:
        detector.actualizar(nuevos)
    for clave, valor in nuevos.items():
        setattr(config, clave, valor)

    # Cambio de cámara EN CALIENTE (después de todo lo demás).
    # Si falla, cambia_fuente revierte la config y propaga el error.
    if nueva_fuente is not None or nuevo_tipo is not None:
        if monitor is None:
            # Sin monitor no se puede recrear: solo guardar el valor
            if nuevo_tipo is not None:
                config.fuente = nuevo_tipo
            if nueva_fuente is not None:
                config.camara_fuente = nueva_fuente
        else:
            monitor.cambiar_fuente(
                nueva_fuente if nueva_fuente is not None
                else config.camara_fuente,
                tipo_fuente=nuevo_tipo if nuevo_tipo is not None
                else config.fuente,
            )

    config.guardar()  # persiste en config.yaml
    notificar_config_cambiada()


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


def ultimos_analisis_fs(cantidad=10) -> list:
    """
    Lee los últimos análisis JSON de la IA (carpeta analisis_ia junto a
    la de capturas), ordenados del más reciente al más antiguo.
    """
    cantidad = min(max(int(cantidad), 0), 50)
    dir_analisis = RUTA_CAPTURAS.parent / "analisis_ia"
    if not dir_analisis.exists():
        return []
    archivos = sorted(dir_analisis.glob("analisis_*.json"), reverse=True)
    lista = []
    for archivo in archivos[:cantidad]:
        try:
            datos = json.loads(archivo.read_text(encoding="utf-8"))
            datos["_archivo"] = archivo.name
            lista.append(datos)
        except (json.JSONDecodeError, OSError):
            continue
    return lista


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
            "area_borde": int(ev.get("area_borde", 0)),
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
           border-radius: 5px; font-size: 14px; cursor: pointer;
           transition: transform 0.05s, opacity 0.15s; }
  .boton:active { transform: scale(0.96); }  /* feedback de presión */
  .boton:disabled { opacity: 0.55; cursor: wait; }
  #guardar { background: #2e7d32; color: white; }
  #limpiar { background: #c62828; color: white; }
  #estado { margin-top: 12px; font-size: 14px; color: #81c784; }
  #vibracion { margin-top: 6px; font-size: 14px; }
  /* Formulario de parámetros en vivo */
  #params { margin-top: 8px; max-width: 640px; }
  .param-grid { display: grid; grid-template-columns: repeat(2, 1fr);
                gap: 8px 16px; margin: 8px 0; }
  .param-grid label { font-size: 12px; color: #aaa;
                      display: flex; flex-direction: column; }
  .param-grid input, .param-grid select { margin-top: 2px; padding: 5px 8px;
    background: #262626; color: #eee; border: 1px solid #444;
    border-radius: 5px; font-size: 13px; }
  .param-checks { margin: 8px 0; display: flex; gap: 18px;
                  font-size: 13px; color: #ccc; }
  /* Parámetros que no aplican en el modo actual → atenuados */
  #params input:disabled, #params select:disabled {
    opacity: 0.45; cursor: not-allowed; }
  .param-botones { display: flex; align-items: center; gap: 10px;
                   margin-top: 4px; flex-wrap: wrap; }
  .preset-label { font-size: 13px; color: #ccc; display: flex;
                  align-items: center; gap: 6px; }
  #p-preset { padding: 6px 8px; background: #262626; color: #eee;
              border: 1px solid #444; border-radius: 5px; font-size: 13px; }
  #params-estado { font-size: 13px; min-height: 18px; }
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
  /* Bloque de análisis IA */
  #ia-caja { margin-top: 16px; }
  #ia { max-height: 300px; overflow-y: scroll; border: 2px solid #3a4a5a;
        border-radius: 8px; background: #16222b; padding: 10px;
        font-size: 13px; }
  #ia p { color: #aaa; }
  .ia-item { border: 1px solid #2e7d32; border-radius: 6px; margin-bottom: 8px;
             padding: 8px 12px; background: #1c2b24; color: #c8e6c9; }
  .ia-hora { color: #81c784; font-weight: bold; }
  .ia-kv { display: flex; gap: 6px; margin: 2px 0; }
  .ia-k { color: #81c784; min-width: 90px; }
  .ia-pre { background: #0c1216; border-radius: 4px; padding: 6px;
            overflow-x: auto; color: #b0bec5; white-space: pre-wrap;
            word-break: break-word; }
  /* Historial IA: scroll propio, máximo 10 registros */
  #ia-historial { margin-top: 8px; max-height: 320px; overflow-y: scroll;
                  border: 2px solid #37474f; border-radius: 8px;
                  background: #11191f; padding: 8px; }
  .ia-hist-item { border-bottom: 1px solid #263238; padding: 6px 2px;
                  font-size: 12px; }
  .ia-hist-titulo { color: #81c784; font-weight: bold; }
  #ia-historial p { color: #aaa; }
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
        <button class="boton" id="rot-ccw" style="background:#546e7a"
                title="Rotar 90° en sentido antihorario">↺ 90°</button>
        <button class="boton" id="rot-cw" style="background:#546e7a"
                title="Rotar 90° en sentido horario">↻ 90°</button>
      </div>
      <div id="estado"></div>
      <div id="vibracion"></div>

      <div id="params">
        <h1 style="margin-top:16px">⚙️ Parámetros (en vivo)</h1>
        <p class="info">Cada campo se aplica al terminar de editarlo
           (Enter o clic fuera). El botón 💾 Aplicar aplica todo de una
           vez. Todo se guarda en config.yaml.</p>
        <div class="param-grid">
          <label>Intervalo de sondeo (s)
            <input id="p-intervalo" type="number" step="0.05" min="0.05">
          </label>
          <label>Método
            <select id="p-metodo">
              <option value="ssim">ssim</option>
              <option value="diff">diff</option>
              <option value="mse">mse</option>
            </select>
          </label>
          <label>min_area_px (mín. de cambio)
            <input id="p-min-area" type="number" step="1" min="0"
                   title="No aplica con el método mse">
          </label>
          <label>Umbral (sensibilidad px, 0-1)
            <input id="p-umbral" type="number" step="0.01" min="0" max="1"
                   title="Más bajo = más sensible. En ssim 0.5 ≈ el corte clásico">
          </label>
          <label>Blur (k, impar)
            <input id="p-blur" type="number" step="1" min="0">
          </label>
          <label>Frames estables
            <input id="p-frames" type="number" step="1" min="1">
          </label>
          <label>Min. entre eventos (s)
            <input id="p-intervalo-eventos" type="number" step="0.1" min="0">
          </label>
          <label>Máx. desplazamiento (px)
            <input id="p-max-desp" type="number" step="0.5" min="1"
                   title="Solo aplica si la compensación de vibración está activa">
          </label>
        </div>
        <div class="param-checks">
          <label><input id="p-marcar" type="checkbox"
                 title="No aplica con el método mse"> Marcar cambios</label>
          <label><input id="p-alinear" type="checkbox"> Compensar vibración</label>
        </div>
        <div class="param-botones">
          <label class="preset-label"
                 title="Ajusta min_area_px, blur y max_desplazamiento según la resolución de la cámara (el display ocupa más píxeles)">📷 Preset:
            <select id="p-preset">
              <option value="">— elegir resolución —</option>
            </select>
          </label>
          <button class="boton" id="btn-aplicar" style="background:#1565c0">💾 Aplicar todo</button>
          <button class="boton" id="btn-recargar" style="background:#455a64">🔄 Recargar valores</button>
          <span id="params-estado"></span>
        </div>
      </div>
    </div>

    <div class="col-der">
      <h1 style="margin-top:0">📸 Últimas capturas</h1>
      <div id="ultimas">
        <p>Cargando...</p>
      </div>

      <div id="ia-caja">
        <div class="log-encabezado">
          <h1 style="margin:0">🕵️ Análisis IA (último cambio)</h1>
          <button class="boton" id="btn-ia" style="background:#455a64">ACTUALIZAR</button>
        </div>
        <div id="ia">
          <p>El análisis IA está detenido. Actívalo para ver el análisis de
             cada cambio detectado.</p>
        </div>
        <div id="ia-prompt-blk">
          <label class="preset-label" style="font-size:12px">✨ Prompt de la IA</label>
          <textarea id="ia-prompt" rows="6"
             style="width:100%; box-sizing:border-box; background:#0c1216;
                    color:#b0bec5; border:1px solid #37474f; border-radius:6px;
                    font-family:Consolas,monospace; font-size:12px;
                    padding:6px;"></textarea>
          <div style="margin-top:6px">
            <button class="boton" id="btn-ia-prompt" style="background:#1565c0">💾 Guardar prompt</button>
            <button class="boton" id="btn-ia-prompt-guardar" style="background:#00838f">➕ Guardar en historial</button>
            <button class="boton" id="btn-prompt-hist" style="background:#455a64">📚 Prompts guardados</button>
            <span id="ia-prompt-estado" style="font-size:12px;color:#aaa;margin-left:8px"></span>
          </div>
        </div>
        <div style="margin-top:8px">
          <button class="boton" id="btn-historial" style="background:#37474f">📜 Mostrar registro</button>
        </div>
        <div id="ia-historial" style="display:none"></div>
      </div>

      <!-- Modal: historial de prompts guardados -->
      <div id="modal-prompts" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0;
           background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center;">
        <div style="background:#1f1f1f; border:1px solid #555; border-radius:10px; max-width:720px;
             width:92%; max-height:80%; overflow:hidden; display:flex; flex-direction:column;">
          <div class="log-encabezado" style="padding:12px 16px; border-bottom:1px solid #333;">
            <span style="font-size:16px; font-weight:bold">📚 Prompts guardados (máx 10)</span>
            <button class="boton" id="btn-cerrar-prompts" style="background:#c62828">✖ Cerrar</button>
          </div>
          <div id="lista-prompts" style="padding:10px 16px; overflow-y:auto;"></div>
        </div>
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

// Los streams de video (MJPEG/RTSP) no disparan 'load' de archivo cada
// vez, y la resolución/caja del <img> puede cambiar. Re-sincronizamos el
// canvas con la caja real de la imagen periódicamente para que el dibujo
// del ROI coincida siempre con lo que se ve.
setInterval(() => {
  const cw = imagen.clientWidth, ch = imagen.clientHeight;
  if ((cw <= 0) || (lienzo.width === cw && lienzo.height === ch)) return;
  lienzo.width = cw;
  lienzo.height = ch;
  dibujar();
}, 400);

// Resolución real del stream (informada por el backend en /api/config).
// Se usa para el mapeo ROI↔pantalla en lugar de naturalWidth/Height,
// que en streams MJPEG/RTSP pueden estar desincronizados y desplazan o
// encogen el rectángulo.
let _vW = 640, _vH = 480;
function setResolucionVideo(w, h) {
  if (w > 0 && h > 0) { _vW = w; _vH = h; }
}

// Coordenadas de pantalla (css, sobre lienzo) → píxeles reales del video.
// Usa un único factor de escala según la caja visible de la imagen.
function px_video_x(x) {
  return Math.round(x * (_vW / imagen.clientWidth));
}
function px_video_y(y) {
  return Math.round(y * (_vH / imagen.clientHeight));
}

function dibujar() {
  if (!imagen.clientWidth) return;
  ctx.clearRect(0, 0, lienzo.width, lienzo.height);
  if (!roi) return;
  // roi está en píxeles del video real → a coordenadas del lienzo (css)
  const x = roi[0] / _vW * lienzo.width;
  const y = roi[1] / _vH * lienzo.height;
  const w = roi[2] / _vW * lienzo.width;
  const h = roi[3] / _vH * lienzo.height;
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
  // Convertir ambos extremos (css) a píxeles del video real
  const aX = px_video_x(inicioX), aY = px_video_y(inicioY);
  const bX = px_video_x(x), bY = px_video_y(y);
  roi = [Math.min(aX, bX), Math.min(aY, bY),
         Math.abs(bX - aX), Math.abs(bY - aY)];
  dibujar();
});

lienzo.addEventListener('mouseup', () => { dibujando = false; });

document.getElementById('guardar').addEventListener('click', async () => {
  if (!roi || roi[2] < 10 || roi[3] < 10) {
    estado('⚠️ Dibuja primero un rectángulo sobre el video.');
    return;
  }
  // roi ya está en píxeles del video real (ver mousemove)
  const region = roi.map(v => Math.round(v));
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

// ── Rotación de imagen (0/90/180/270) ──────────────────────────────
let rotAct = 0;
// Lee la rotación actual desde la config al cargar
async function leerRotacion() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    if (d.captura && typeof d.captura.rotacion === 'number') {
      rotAct = d.captura.rotacion;
      estado('🔄 Rotación ' + rotAct + '°');
    }
  } catch (e) {}
}
async function aplicarRotacion(nueva) {
  const ok = await fetch('/api/rotacion', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rotacion: nueva}),
  });
  if (ok.ok) { rotAct = nueva; estado('🔄 Rotación ' + nueva + '°'); }
}
document.getElementById('rot-ccw').addEventListener('click', () => {
  aplicarRotacion((rotAct + 270) % 360);  // antihorario = -90
});
document.getElementById('rot-cw').addEventListener('click', () => {
  aplicarRotacion((rotAct + 90) % 360);
});

function estado(msg) { document.getElementById('estado').textContent = msg; }

// ── Indicador de vibración (compensación de imágenes) ────────────────
// Muestra en vivo el desplazamiento estimado por la alineación.
// Con cámara estable muestra "sin vibración"; al golpear el soporte
// o vibrar la cámara muestra el desplazamiento que se está compensando.
const fuenteEstado = new EventSource('/api/estado');
fuenteEstado.onmessage = (ev) => {
  const d = JSON.parse(ev.data);
  const el = document.getElementById('vibracion');
  if (!d.alinear) {
    // Estado explícito: así nunca hay duda de si está activa o no
    el.textContent = '🚫 Compensación de vibración DESACTIVADA';
    el.style.color = '#aaa';
    return;
  }
  if (d.activo) {
    el.textContent = `⚠️ Vibración: dy=${d.dy}, dx=${d.dx} px · margen ${d.margen}px · cambio ${d.area_interior}px interior / ${d.area_borde}px borde`;
    el.style.color = '#ffb74d';
  } else {
    el.textContent = `✅ Sin vibración (dy=${d.dy}, dx=${d.dx} px) · margen ${d.margen}px · cambio ${d.area_interior}px interior / ${d.area_borde}px borde`;
    el.style.color = '#81c784';
  }
};
fuenteEstado.onerror = () => { /* EventSource reconecta solo */ };

// ── Parámetros en vivo ────────────────────────────────────────────────
async function cargarParams() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    document.getElementById('p-intervalo').value = cfg.captura.intervalo_segundos;
    document.getElementById('p-metodo').value = cfg.deteccion.metodo;
    document.getElementById('p-min-area').value = cfg.deteccion.min_area_px;
    document.getElementById('p-umbral').value = cfg.deteccion.umbral;
    document.getElementById('p-blur').value = cfg.deteccion.blur_ksize;
    document.getElementById('p-frames').value = cfg.deteccion.frames_estables;
    document.getElementById('p-intervalo-eventos').value = cfg.deteccion.min_intervalo_eventos;
    document.getElementById('p-max-desp').value = cfg.deteccion.max_desplazamiento;
    document.getElementById('p-marcar').checked = cfg.deteccion.marcar_cambios;
    document.getElementById('p-alinear').checked = cfg.deteccion.alinear_imagenes;
    actualizarHabilitados();
  } catch (e) { /* si falla, dejar los valores por defecto */ }
}

function num(id) { return document.getElementById(id).value; }

// Habilita/deshabilita campos según el método y la vibración:
// un parámetro solo se edita cuando realmente tiene efecto.
function actualizarHabilitados() {
  const esMse = document.getElementById('p-metodo').value === 'mse';
  const alinear = document.getElementById('p-alinear').checked;
  document.getElementById('p-min-area').disabled = esMse;
  document.getElementById('p-marcar').disabled = esMse;
  document.getElementById('p-max-desp').disabled = !alinear;
  // `umbral` siempre aplica: mse lo usa como corte de score,
  // ssim/diff como sensibilidad a nivel píxel.
}

async function aplicar(cuerpo, mensajeExito) {
  const estadoEl = document.getElementById('params-estado');
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cuerpo),
    });
    const d = await res.json();
    if (d.ok) {
      estadoEl.textContent = mensajeExito || '✅ Aplicado y guardado en config.yaml';
      estadoEl.style.color = '#81c784';
      return true;
    } else {
      estadoEl.textContent = '❌ ' + (d.error || 'Error');
      estadoEl.style.color = '#ff8a80';
      return false;
    }
  } catch (e) {
    estadoEl.textContent = '❌ Error de conexión';
    estadoEl.style.color = '#ff8a80';
    return false;
  }
}

// Los checkboxes se aplican SOLOS al marcarlos/desmarcarlos (parcial)
document.getElementById('p-marcar').addEventListener('change', () => {
  const activo = document.getElementById('p-marcar').checked;
  aplicar({deteccion: {marcar_cambios: activo}},
          activo ? '✅ Marcar cambios ACTIVADO' : '✅ Marcar cambios DESACTIVADO');
});
document.getElementById('p-alinear').addEventListener('change', () => {
  const activo = document.getElementById('p-alinear').checked;
  aplicar({deteccion: {alinear_imagenes: activo}},
          activo ? '✅ Compensación de vibración ACTIVADA' : '✅ Compensación de vibración DESACTIVADA');
  actualizarHabilitados();
});

// Cada campo numérico/select se aplica SOLO al terminar de editarlo
// (evento change = Enter o clic fuera). Así el valor visible siempre
// coincide con el aplicado, sin depender del botón.
const CAMPOS = [
  ['p-intervalo', 'captura', 'intervalo_segundos', parseFloat],
  ['p-metodo', 'deteccion', 'metodo', v => v],
  ['p-umbral', 'deteccion', 'umbral', parseFloat],
  ['p-min-area', 'deteccion', 'min_area_px', v => parseInt(v, 10)],
  ['p-blur', 'deteccion', 'blur_ksize', v => parseInt(v, 10)],
  ['p-frames', 'deteccion', 'frames_estables', v => parseInt(v, 10)],
  ['p-intervalo-eventos', 'deteccion', 'min_intervalo_eventos', parseFloat],
  ['p-max-desp', 'deteccion', 'max_desplazamiento', parseFloat],
];
for (const [id, seccion, clave, conv] of CAMPOS) {
  document.getElementById(id).addEventListener('change', () => {
    const v = conv(document.getElementById(id).value);
    const estadoEl = document.getElementById('params-estado');
    if (Number.isNaN(v)) {
      estadoEl.textContent = '⚠️ Valor incompleto — termina de escribir y pulsa Enter';
      estadoEl.style.color = '#ffb74d';
      return;
    }
    aplicar({[seccion]: {[clave]: v}});
  });
}

// Al cambiar el método, re-evaluar qué campos quedan editables
document.getElementById('p-metodo').addEventListener('change', actualizarHabilitados);

// Presets por resolución de cámara: reemplazan al botón de valores
// por defecto con configuraciones calibradas según los megapíxeles.
async function cargarPresets() {
  try {
    const res = await fetch('/api/config');
    const cfg = await res.json();
    const sel = document.getElementById('p-preset');
    sel.innerHTML = '<option value="">— elegir resolución —</option>' +
      (cfg.presets || []).map(p =>
        `<option value="${p.id}">${p.nombre} (${p.ancho}x${p.alto})</option>`
      ).join('');
    window._presets = cfg.presets || [];
    // Resolución detectada del stream: seleccionar el preset aplicado
    if (cfg.camara_resolucion) {
      const [w, h] = cfg.camara_resolucion;
      setResolucionVideo(w, h);
      const p = (cfg.presets || []).find(x => x.id === cfg.preset_aplicado);
      const estadoEl = document.getElementById('params-estado');
      if (p) {
        sel.value = p.id;
        estadoEl.textContent = `📷 Resolución ${w}x${h} → preset ${p.nombre} aplicado`;
      } else {
        estadoEl.textContent = `📷 Resolución ${w}x${h} detectada (sin preset)`;
      }
      estadoEl.style.color = '#aaa';
    }
  } catch (e) { /* mantener el select vacío */ }
}

document.getElementById('p-preset').addEventListener('change', async () => {
  const sel = document.getElementById('p-preset');
  const preset = (window._presets || []).find(p => p.id === sel.value);
  if (!preset) return;
  if (!confirm(`¿Aplicar el preset de ${preset.nombre}?\nAjustará min_area_px, blur y max_desplazamiento (y dejará ssim, umbral 0.5, frames 2).`)) {
    sel.value = '';
    return;
  }
  const ok = await aplicar({deteccion: preset.deteccion},
                           `✅ Preset ${preset.nombre} aplicado`);
  // Mantener la opción elegida visible en el select (no volver al
  // placeholder). Si se cancela la confirmación, sí se revierte.
  if (ok) cargarParams();
});

// Recargar los valores aplicados desde el servidor (verdad en vivo),
// por si otro cliente o edición manual cambió algo
const btnRecargar = document.getElementById('btn-recargar');
if (btnRecargar) {
  btnRecargar.addEventListener('click', () => {
    cargarParams();
    document.getElementById('params-estado').textContent = '🔄 Valores recargados desde el servidor';
    document.getElementById('params-estado').style.color = '#aaa';
  });
}

document.getElementById('btn-aplicar').addEventListener('click', async () => {
  const btn = document.getElementById('btn-aplicar');
  const etiquetaOriginal = '💾 Aplicar todo';
  const colorOriginal = '#1565c0';
  const cuerpo = {
    captura: { intervalo_segundos: parseFloat(num('p-intervalo')) },
    deteccion: {
      metodo: num('p-metodo'),
      umbral: parseFloat(num('p-umbral')),
      min_area_px: parseInt(num('p-min-area'), 10),
      blur_ksize: parseInt(num('p-blur'), 10),
      frames_estables: parseInt(num('p-frames'), 10),
      min_intervalo_eventos: parseFloat(num('p-intervalo-eventos')),
      max_desplazamiento: parseFloat(num('p-max-desp')),
      marcar_cambios: document.getElementById('p-marcar').checked,
      alinear_imagenes: document.getElementById('p-alinear').checked,
    },
  };

  // Estado "guardando": botón deshabilitado para evitar doble envío
  btn.disabled = true;
  btn.textContent = '⏳ Guardando...';
  const ok = await aplicar(cuerpo);
  btn.disabled = false;

  // Feedback claro en el PROPIO botón: verde = éxito, rojo = error
  if (ok) {
    btn.style.background = '#2e7d32';
    btn.textContent = '✅ Guardado';
    cargarParams();  // resincroniza el formulario con lo aplicado
  } else {
    btn.style.background = '#c62828';
    btn.textContent = '❌ Error';
  }
  setTimeout(() => {
    btn.style.background = colorOriginal;
    btn.textContent = etiquetaOriginal;
  }, 1800);
});

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
      const borde = ev.area_borde > 0 ? ` | ⚠️ ${ev.area_borde}px eran de borde` : '';
      return `<div class="log-item">
        <div class="log-hora">🕐 ${hora}</div>
        <div class="log-metricas">
          Cambio: <b>${ev.area_px} píxeles</b>${borde} | score: ${ev.score} | umbral actual: ${d.min_area_px}px
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

// ── Análisis IA ────────────────────────────────────────────────────
let iaActiva = false;

// Pinta el botón SEGÚN el estado (sin red). Fuente de verdad: iaActiva.
function pintaBotonIA(activa) {
  iaActiva = activa;
  const btn = document.getElementById('btn-ia');
  if (!btn) return;
  btn.textContent = activa ? '⏹️ Detener análisis IA' : '▶️ Activar análisis IA';
  btn.style.background = activa ? '#c62828' : '#2e7d32';
}

// Lee el estado real al cargar (después de pintar con lo actualizado).
async function actualizarEstadoIA() {
  try {
    const r = await fetch('/api/ia');
    const est = await r.json();
    pintaBotonIA(!!est.enabled);
  } catch (e) { /* dejar el estado actual */ }
}

document.getElementById('btn-ia').addEventListener('click', async () => {
  const proximo = !iaActiva;
  // Pintar al instante (optimista): siempre alterna al hacer clic
  pintaBotonIA(proximo);
  try {
    const r = await fetch('/api/ia', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: proximo}),
    });
    const d = await r.json();
    // Corregir según lo que confirme el servidor (si difiere)
    if (d && typeof d.enabled === 'boolean' && d.enabled !== proximo) {
      pintaBotonIA(d.enabled);
    }
  } catch (e) {
    // Si el POST falla (red/404), revertir al estado previo
    pintaBotonIA(!proximo);
  }
});

async function actualizarAnalisis() {
  let lista;
  try {
    const res = await fetch('/api/analisis');
    const d = await res.json();
    lista = (d.analisis || []).slice(0, 10);
  } catch (e) { return; }

  // Último (más reciente) análisis completo
  const ultimo = document.getElementById('ia');
  if (!lista.length) {
    ultimo.innerHTML = '<p>Sin análisis aún — cuando se detecte un cambio con IA activa, el JSON aparecerá aquí.</p>';
  } else {
    const a = lista[0];
    const hora = (a.timestamp || '').replace('T', ' ').slice(0, 19);
    const { _archivo, evento_id, timestamp: _ts, imagen_original: _img, ...resto } = a;
    ultimo.innerHTML = `<div class="ia-item">
      <div class="ia-hora">🕐 ${hora} · evento ${evento_id || ''}</div>
      <pre class="ia-pre">${JSON.stringify(resto, null, 2)}</pre>
    </div>`;
  }

  // Historial completo (hasta 10) para el bloque desplegable
  const hist = document.getElementById('ia-historial');
  if (!lista.length) {
    hist.innerHTML = '<p>Sin registros de análisis aún.</p>';
    return;
  }
  hist.innerHTML = lista.map(a => {
    const hora = (a.timestamp || '').replace('T', ' ').slice(0, 19);
    const { _archivo, timestamp: _ts, imagen_original: _img, ...dato } = a;
    // dato = el JSON completo que devolvió la IA (sin metadatos)
    return `<div class="ia-hist-item">
      <span class="ia-hist-titulo">🕐 ${hora} · evento ${a.evento_id || ''}</span>
      <pre class="ia-pre" style="margin-top:4px">${JSON.stringify(dato, null, 2)}</pre>
    </div>`;
  }).join('');
  if (hist.style.display !== 'none') hist.scrollTop = 0;
}

// Toggle del bloque de registro/historial (oculto por defecto)
document.getElementById('btn-historial').addEventListener('click', () => {
  const hist = document.getElementById('ia-historial');
  const btn = document.getElementById('btn-historial');
  const oculto = hist.style.display === 'none';
  hist.style.display = oculto ? 'block' : 'none';
  btn.textContent = oculto ? '🙈 Ocultar registro' : '📜 Mostrar registro';
});

// ── Prompt de la IA (editable en tiempo real) ─────────────────────
async function cargarPromptIA() {
  try {
    const r = await fetch('/api/ia/prompt');
    const d = await r.json();
    document.getElementById('ia-prompt').value = d.prompt || '';
  } catch (e) { /* dejar vacío */ }
}

document.getElementById('btn-ia-prompt').addEventListener('click', async () => {
  const texto = document.getElementById('ia-prompt').value;
  const estadoEl = document.getElementById('ia-prompt-estado');
  try {
    const r = await fetch('/api/ia/prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: texto}),
    });
    const d = await r.json();
    if (d.ok) {
      estadoEl.textContent = '✅ Guardado'; estadoEl.style.color = '#81c784';
    } else {
      estadoEl.textContent = '❌ ' + (d.error || 'Error'); estadoEl.style.color = '#ff8a80';
    }
  } catch (e) {
    estadoEl.textContent = '❌ Error de conexión'; estadoEl.style.color = '#ff8a80';
  }
});

// ── Historial de prompts (guardar / reutilizar) ────────────────────
// Guardar el prompt ACTUAL al historial (no reemplaza al aplicado)
document.getElementById('btn-ia-prompt-guardar').addEventListener('click', async () => {
  const texto = document.getElementById('ia-prompt').value;
  const estadoEl = document.getElementById('ia-prompt-estado');
  if (!texto.trim()) { estadoEl.textContent = '⚠️ Prompt vacío'; estadoEl.style.color='#ffb74d'; return; }
  try {
    await fetch('/api/ia/prompts-hist', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: texto}),
    });
    estadoEl.textContent = '✅ Guardado en historial'; estadoEl.style.color = '#81c784';
  } catch (e) { estadoEl.textContent = '❌ Error'; estadoEl.style.color = '#ff8a80'; }
});

document.getElementById('btn-prompt-hist').addEventListener('click', () => {
  document.getElementById('modal-prompts').style.display = 'flex';
  refrescarListaPrompts();
});
document.getElementById('btn-cerrar-prompts').addEventListener('click', () => {
  document.getElementById('modal-prompts').style.display = 'none';
});

async function refrescarListaPrompts() {
  let lista = [];
  try {
    const r = await fetch('/api/ia/prompts-hist');
    const d = await r.json();
    lista = d.prompts || [];
  } catch (e) {}
  const cont = document.getElementById('lista-prompts');
  if (!lista.length) {
    cont.innerHTML = '<p style="color:#aaa">Sin prompts guardados aún.</p>';
    return;
  }
  cont.innerHTML = lista.map((p, idx) => {
    const preview = p.replace(/\s+/g, ' ').slice(0, 90) + (p.length > 90 ? '…' : '');
    return `<div class="ia-hist-item">
      <div><b style="color:#00c48c">#${idx + 1}</b> · ${preview}</div>
      <div style="margin-top:6px">
        <button class="boton" data-usar="${idx}" style="background:#00695c;font-size:12px;padding:4px 10px">▶ Usar</button>
        <button class="boton" data-copiar="${idx}" style="background:#37474f;font-size:12px;padding:4px 10px">📋 Copiar</button>
        <button class="boton" data-borrar="${idx}" style="background:#c62828;font-size:12px;padding:4px 10px">🗑</button>
      </div>
    </div>`;
  }).join('');
  window._listaPrompts = lista;

  cont.querySelectorAll('[data-usar]').forEach(b => b.addEventListener('click', async () => {
    const elegido = window._listaPrompts[+b.dataset.usar];
    // Aplicar como prompt actual (guarda en el textarea y en el server)
    document.getElementById('ia-prompt').value = elegido;
    await fetch('/api/ia/prompt', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({prompt: elegido})});
    document.getElementById('modal-prompts').style.display = 'none';
    document.getElementById('ia-prompt-estado').textContent = '✅ Prompt aplicado'; document.getElementById('ia-prompt-estado').style.color='#81c784';
  }));
  cont.querySelectorAll('[data-copiar]').forEach(b => b.addEventListener('click', () => {
    const elegido = window._listaPrompts[+b.dataset.copiar];
    document.getElementById('ia-prompt').value = elegido;
  }));
  cont.querySelectorAll('[data-borrar]').forEach(b => b.addEventListener('click', async () => {
    const elegido = window._listaPrompts[+b.dataset.borrar];
    await fetch('/api/ia/prompts-hist/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({prompt: elegido})});
    refrescarListaPrompts();
  }));
}

// Cargar al abrir la página
actualizarUltimas();
actualizarLog();
cargarParams();
cargarPresets();
actualizarEstadoIA();
actualizarAnalisis();
cargarPromptIA();
leerRotacion();

// Actualizar SOLO cuando el servidor avisa que hay una captura nueva
// (sin polling periódico)
const fuenteEventos = new EventSource('/api/eventos');
fuenteEventos.onmessage = () => { actualizarUltimas(); actualizarLog(); };
fuenteEventos.onerror = () => {
  // Si la conexión se corta, EventSource reconecta solo; nada que hacer.
  console.log('Conexión de eventos reconectando...');
};

// Aviso cuando hay un análisis IA nuevo
const fuenteAnalisis = new EventSource('/api/eventos-analisis');
fuenteAnalisis.onmessage = () => { actualizarAnalisis(); };
fuenteAnalisis.onerror = () => { /* reconecta solo */ };

// Aviso cuando la CONFIGURACIÓN cambió (por el panel o por un cliente
// externo vía API): se refrescan los valores del formulario y la
// rotación, sin recargar la página.
const fuenteConfig = new EventSource('/api/config-eventos');
fuenteConfig.onmessage = () => {
  // No pisar lo que el usuario está editando en este momento
  const activo = document.activeElement;
  const editando = activo && (activo.tagName === 'INPUT' ||
                              activo.tagName === 'TEXTAREA' ||
                              activo.tagName === 'SELECT');
  if (editando) return;
  cargarParams(); leerRotacion(); cargarPresets();
};
fuenteConfig.onerror = () => { /* reconecta solo */ };

imagen.src = '/video';
</script>
</body>
</html>
"""


def crear_app(capturador, config=None, detector=None, monitor=None):
    """Crea la aplicación Flask conectada al capturador activo.

    `detector` es opcional: si se pasa, el panel muestra en vivo el
    desplazamiento estimado por la compensación de vibración.
    `monitor` es el MonitorBackend (opcional): permite cambiar la fuente
    de cámara en caliente desde la API sin reiniciar el proceso.
    """
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
                # Aplicar la rotación configurada para que el panel, el
                # ROI y la IA vean la misma orientación
                rot = getattr(config, "rotacion", 0) if config is not None else 0
                if rot:
                    from backend.capturador import rotar_frame
                    frame = rotar_frame(frame, rot)
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
        de qué parámetro ajustar para el filtro. El umbral se lee EN
        VIVO de la config (no del arranque), para que las sugerencias
        reflejen los cambios hechos desde el panel.
        """
        umbral_area = config.min_area_px if config is not None else min_area
        return jsonify({
            "min_area_px": umbral_area,
            "eventos": construir_log_web(ruta_log, umbral_area),
        })

    @app.route("/api/config")
    def api_config():
        """Parámetros actuales + presets por resolución para el panel web."""
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        datos = config.a_dict()
        datos["presets"] = PRESETS_CAMARA
        # Resolución real del stream y preset aplicado (info de runtime,
        # no se persiste en el YAML)
        datos["camara_resolucion"] = getattr(config, "camara_resolucion", None)
        datos["preset_aplicado"] = getattr(config, "preset_aplicado", None)
        return jsonify(datos)

    @app.route("/api/config", methods=["POST"])
    def api_config_guardar():
        """
        Aplica parámetros EN CALIENTE (sin reiniciar) y los persiste
        en config.yaml. Acepta {"captura": {...}, "deteccion": {...}}.

        Si cambia `camara_fuente` o `fuente`, recrea el capturador al
        instante (si la cámara nueva no abre, mantiene la anterior y
        devuelve error).
        """
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        datos = request.get_json(silent=True) or {}
        try:
            _aplicar_config(datos, config, detector, monitor=monitor)
            return jsonify({"ok": True})
        except RuntimeError as e:
            # Falló el cambio de cámara: el capturador anterior sigue vivo
            return jsonify({"ok": False, "error": str(e)}), 502
        except (ValueError, TypeError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400

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

    @app.route("/api/rotacion", methods=["POST"])
    def api_rotacion():
        """Fija la rotación de la imagen (0/90/180/270, en el sentido de
        las agujas del reloj en la vista del panel). Aplica en vivo."""
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        datos = request.get_json(silent=True) or {}
        try:
            ang = int(datos.get("rotacion", 0)) % 360
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "rotación inválida"}), 400
        config.rotacion = ang
        try:
            config.guardar()
        except OSError:
            pass
        logger.info(f"🔄 Rotación de imagen establecida a {ang}°")
        return jsonify({"ok": True, "rotacion": ang})

    @app.route("/api/analisis")
    def api_analisis():
        """Últimos análisis de IA (historial), del más reciente al más
        antiguo. Máximo 10 registros."""
        return jsonify({"analisis": ultimos_analisis_fs(10)})

    @app.route("/api/ia")
    def api_ia_estado():
        """Estado actual del análisis IA (enabled/disabled)."""
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        return jsonify({"enabled": bool(config.ia_enabled),
                        "model": config.ia_model,
                        "detail": config.ia_detail})

    @app.route("/api/ia", methods=["POST"])
    def api_ia_toggle():
        """Activa/desactiva el análisis IA EN SESIÓN (no se persiste:
        el backend arranca siempre con IA desactivada por defecto)."""
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        datos = request.get_json(silent=True) or {}
        if "enabled" not in datos:
            return jsonify({"ok": False, "error": "falta 'enabled'"}), 400
        config.ia_enabled = bool(datos["enabled"])
        logger.info("🕵️ Análisis IA %s",
                    "ACTIVADO (sesión)" if config.ia_enabled else "DETENIDO")
        return jsonify({"ok": True, "enabled": bool(config.ia_enabled)})

    @app.route("/api/ia/prompt")
    def api_ia_prompt_get():
        """Devuelve el prompt actual del análisis IA."""
        from backend.ia import lee_prompt_actual
        return jsonify({"prompt": lee_prompt_actual()})

    @app.route("/api/ia/prompt", methods=["POST"])
    def api_ia_prompt_post():
        """Guarda el prompt del análisis IA (se aplica en tiempo real)."""
        from backend.ia import guarda_prompt
        datos = request.get_json(silent=True) or {}
        texto = datos.get("prompt", "")
        if not texto.strip():
            return jsonify({"ok": False, "error": "prompt vacío"}), 400
        guarda_prompt(texto)
        logger.info("🕵️ Prompt IA actualizado")
        return jsonify({"ok": True})

    @app.route("/api/ia/prompts-hist")
    def api_ia_historial():
        """Lista de prompts guardados (máx 10), más reciente primero."""
        from backend.ia import lista_prompts_hist
        return jsonify({"prompts": lista_prompts_hist()})

    @app.route("/api/ia/prompts-hist", methods=["POST"])
    def api_ia_historial_guardar():
        """Guarda el prompt actual (o el enviado) en el historial."""
        from backend.ia import guarda_prompt_historico
        datos = request.get_json(silent=True) or {}
        texto = datos.get("prompt", "").strip()
        if not texto:
            # si no viene, usar el del archivio actual
            from backend.ia import lee_prompt_actual
            texto = lee_prompt_actual()
        lista = guarda_prompt_historico(texto)
        return jsonify({"ok": True, "prompts": lista})

    @app.route("/api/ia/prompts-hist/delete", methods=["POST"])
    def api_ia_historial_eliminar():
        """Elimina un prompt del historial por contenido."""
        from backend.ia import elimina_prompt_historico
        datos = request.get_json(silent=True) or {}
        texto = datos.get("prompt", "")
        lista = elimina_prompt_historico(texto)
        return jsonify({"ok": True, "prompts": lista})

    @app.route("/api/estado-sistema")
    def api_estado_sistema():
        """
        ESTADO ACTUAL completo (solo lectura): parámetros de
        configuración + datos de runtime. Pensado para clientes externos
        que quieran conocer qué está pasando sin pedir la imagen.
        """
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        camara_viva = False
        capturas = cambios = 0
        if monitor is not None:
            capturas = getattr(monitor, "conteo_capturas", 0)
            cambios = getattr(monitor, "conteo_cambios", 0)
            try:
                capturador = getattr(monitor, "capturador", None)
                if capturador is not None:
                    capturador.capturar()  # lanza si no hay frames
                    camara_viva = True
            except Exception:  # noqa: BLE001
                camara_viva = False
        return jsonify({
            "config": config.a_dict(),
            "runtime": {
                "camara_viva": camara_viva,
                "camara_resolucion": getattr(config, "camara_resolucion", None),
                "preset_aplicado": getattr(config, "preset_aplicado", None),
                "rotacion_efectiva": config.rotacion,
                "roi": cargar_roi(),
                "ia_activa": bool(config.ia_enabled),
                "capturas": capturas,
                "cambios": cambios,
            },
        })

    @app.route("/api/config-eventos")
    def api_config_eventos():
        """SSE: avisa al panel cuando la configuración cambió (por el
        propio panel o por un cliente externo), para que refresque sus
        valores sin recargar la página."""
        def generar():
            ultimo = _contador_config
            yield ": conectado\n\n"
            while True:
                with _condicion_config:
                    _condicion_config.wait(timeout=30)
                    cambio = _contador_config != ultimo
                    if cambio:
                        ultimo = _contador_config
                if cambio:
                    yield f"data: {ultimo}\n\n"

        return Response(generar(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/eventos-analisis")
    def api_eventos_analisis():
        """SSE: avisa al dashboard cuando hay un análisis IA nuevo."""
        def generar():
            ultimo = _contador_analisis
            yield ": conectado\n\n"
            while True:
                with _condicion_analisis:
                    _condicion_analisis.wait(timeout=30)
                    cambio = _contador_analisis != ultimo
                    if cambio:
                        ultimo = _contador_analisis
                if cambio:
                    yield f"data: {ultimo}\n\n"

        return Response(generar(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    @app.route("/api/estado")
    def api_estado():
        """
        SSE: desplazamiento estimado por la compensación de vibración,
        para ver en vivo si la alineación está actuando. Si la vibración
        está desactivada, envía un único mensaje indicándolo.
        """
        def generar():
            ultimo_valor = None
            while True:
                if detector is not None and getattr(
                        detector, "alinear_imagenes", False):
                    desp = getattr(detector, "ultimo_desplazamiento", None)
                    dy = round(float(desp[0]), 2) if desp else 0.0
                    dx = round(float(desp[1]), 2) if desp else 0.0
                    valor = {
                        "alinear": True,
                        "dy": dy,
                        "dx": dx,
                        "activo": (dy * dy + dx * dx) ** 0.5 > 0.3,
                        "margen": int(getattr(detector, "ultimo_margen", 0)),
                        "area_interior": int(getattr(
                            detector, "ultimo_area_interior", 0)),
                        "area_borde": int(getattr(
                            detector, "ultimo_area_borde", 0)),
                    }
                else:
                    valor = {"alinear": False}
                if valor != ultimo_valor:
                    ultimo_valor = valor
                    yield f"data: {json.dumps(valor)}\n\n"
                time.sleep(1.0)

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

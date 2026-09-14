"""API HTTP del worker (JSON + stream de video).

El worker ya NO sirve el panel HTML: el único punto de entrada para el
usuario es el panel del concentrador, que hace proxy hacia acá. Este módulo
expone:

  • `/video`            stream MJPEG en vivo (con el ROI dibujado encima)
  • `/api/config`       parámetros del detector (leer y escribir en vivo)
  • `/api/roi`          área de análisis (leer, definir, borrar)
  • `/api/ultimas`      últimas capturas
  • `/api/log`          log de eventos (leer y limpiar)
  • `/api/ia*`          análisis IA: estado, prompt, historial
  • `/api/analisis`     historial de análisis de IA
  • `/api/estado*`      estado del sistema, SSE de config y de análisis
  • `/capturas/<n>`     imágenes de eventos
  • `/api/externo/config`  configuración enviada por el concentrador

El ROI se guarda en `roi.json` y el detector lo recarga en cada ciclo, así
los cambios se aplican sin reiniciar el proceso.
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


def _aplicar_config(datos: dict, config, detector, monitor=None,
                    permitir_fuente: bool = True):
    """Valida y aplica captura/detección en caliente, y persiste en YAML.

    `permitir_fuente=True` (panel/front LOCAL): acepta `camara_fuente` y
    `fuente` y recrea el capturador en vivo.
    `permitir_fuente=False` (clientes EXTERNOS: nube/concentrador): esos
    campos se IGNORAN a propósito, porque la URL de la cámara es hardware
    local y no debe cambiarse desde afuera.

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
    if "worker_id" in c:
        # Identidad del worker (viaja en el contrato hacia la nube)
        config.worker_id = str(c["worker_id"]).strip()

    # Cambio de fuente de cámara: se aplica al final (recrea el capturador).
    # SOLO se permite desde el front LOCAL: la URL de la cámara es hardware
    # de esta máquina y no debe poder cambiarse desde la nube/API externa.
    nueva_fuente = None
    nuevo_tipo = None
    if not permitir_fuente:
        if "fuente" in c or "camara_fuente" in c:
            logger.warning(
                "Cambio de cámara IGNORADO: solo se permite desde el panel "
                "local (no desde clientes externos)."
            )
    else:
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

    # ── IA (prompt/esquema/modelo) ──
    ia = datos.get("ia") or {}
    if "esquema" in ia:
        config.ia_esquema = str(ia["esquema"]).strip() or "generico_v1"
    if "model" in ia:
        config.ia_model = str(ia["model"]).strip()
    if "detail" in ia:
        d = str(ia["detail"]).strip()
        if d not in ("low", "high", "auto"):
            raise ValueError("detail debe ser low, high o auto")
        config.ia_detail = d
    if "prompt" in ia:
        from backend.ia import guarda_prompt
        texto = str(ia["prompt"])
        if texto.strip():
            guarda_prompt(texto)

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


# NOTA: el panel HTML ya no vive aquí. El worker expone solo su API JSON y
# el video; el panel lo sirve el concentrador desde `compartido/panel.html`.


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

    # El worker YA NO sirve el panel HTML: el único punto de entrada es el
    # panel del concentrador, que hace proxy hacia acá. Se conserva esta ruta
    # apuntando a la API para no romper enlaces antiguos ni el diagnóstico
    # directo cuando se necesita aislar un worker.
    @app.route("/")
    def raiz():
        return jsonify({
            "servicio": "worker de deteccion de cambios",
            "worker_id": (getattr(config, "worker_id_efectivo", None)
                          and config.worker_id_efectivo()),
            "panel": "El panel se sirve desde el concentrador",
            "api": "/api/estado-sistema",
        })

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

    @app.route("/api/externo/config", methods=["POST"])
    def api_externo_config():
        """
        Endpoint para CLIENTES EXTERNOS (concentrador / nube).

        Aplica configuración en vivo, pero **NO permite cambiar la cámara**
        (`camara_fuente`/`fuente` se ignoran): la URL de cámara es hardware
        local y solo se ajusta desde el panel local.
        """
        if config is None:
            return jsonify({"error": "Configuración no disponible"}), 503
        datos = request.get_json(silent=True) or {}
        try:
            _aplicar_config(datos, config, detector, monitor=monitor,
                            permitir_fuente=False)
            return jsonify({"ok": True})
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

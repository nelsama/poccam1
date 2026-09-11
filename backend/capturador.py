"""Capturadores de imagen: pantalla, cámara USB/IP y stream HTTP MJPEG."""

import threading
import time
from typing import Union

import numpy as np


def rotar_frame(img, grados):
    """Rota la imagen en pasos de 90°. grados ∈ {0,90,180,270}, tomado
    en el sentido de las agujas del reloj (horario), que es el más
    intuitivo: 90 = gira a la derecha, 270 = gira a la izquierda."""
    g = int(grados) % 360
    if g == 0 or img is None:
        return img
    import cv2
    if g == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if g == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if g == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


class CapturadorPantalla:
    """Captura una región de la pantalla (mss — muy rápido)."""

    def __init__(self, region: list | None = None, monitor: int = 1):
        from mss import MSS
        self.sct = MSS()
        self.monitor = monitor
        self.region = region

    def capturar(self) -> np.ndarray:
        if self.region:
            bbox = {
                "left": self.region[0],
                "top": self.region[1],
                "width": self.region[2],
                "height": self.region[3],
                "mon": self.monitor,
            }
        else:
            bbox = self.sct.monitors[self.monitor]
        img = self.sct.grab(bbox)
        return np.array(img)[:, :, :3]  # BGR (los canales BGRA → ignoramos A)

    def cerrar(self):
        self.sct.close()


class CapturadorCamara:
    """Captura desde cámara USB (int) o RTSP (str) vía OpenCV.

    Usa el mismo patrón del CapturadorMJPEG (hilo de fondo + frame más
    reciente) para eliminar la latencia y evitar que `read()` bloquee:

      1. Un hilo en segundo plano lee el stream continuamente (FFMPEG
         para RTSP/red, DirectShow para webcam USB) y se relanza solo
         si el stream falla o se corta (reconexión automática).
      2. Conserva SOLO el último frame válido.
      3. capturar() devuelve al instante el frame más reciente.
         Si el stream se cayó, devuelve el último frame válido mientras
         el hilo intenta reconectar (no muere en cada corte de WiFi).
    """

    def __init__(self, fuente, nombre="camara", timeout_segundos=5):
        self.fuente = fuente
        self.nombre = nombre
        self.timeout = timeout_segundos
        self._frame: np.ndarray | None = None
        self._ultimo_error = None
        self._lock = threading.Lock()
        self._detener = threading.Event()
        self._hilo = threading.Thread(
            target=self._consumir_stream, daemon=True
        )
        self._hilo.start()

        # Esperar el primer frame (con límite de tiempo)
        for _ in range(timeout_segundos * 10):
            with self._lock:
                if self._frame is not None:
                    break
            time.sleep(0.1)
        if self._frame is None:
            # Detener el hilo antes de fallar: si el llamador reintenta,
            # no deben quedar hilos huérfanos abriendo la misma cámara.
            self._detener.set()
            raise RuntimeError(
                f"No se pudo abrir la cámara: {self.fuente} "
                f"({self._ultimo_error})"
            )

    def _abrir_cap(self):
        """Abre el VideoCapture con el backend correcto."""
        import cv2
        es_url = isinstance(self.fuente, str)
        if es_url:
            cap = cv2.VideoCapture(self.fuente, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout * 1000)
        else:
            cap = cv2.VideoCapture(self.fuente, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # solo el frame más reciente
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"No se pudo abrir la cámara: {self.fuente}")
        return cap

    def _consumir_stream(self):
        """Hilo: lee el stream continuamente y conserva el último frame.
        Si el stream se corta, cierra y relanza el VideoCapture."""
        while not self._detener.is_set():
            try:
                cap = self._abrir_cap()
            except Exception as e:  # noqa: BLE001 — reconexión
                self._ultimo_error = str(e)
                if not self._detener.is_set():
                    time.sleep(1.0)  # pausa entre reintentos de apertura
                continue
            while not self._detener.is_set():
                try:
                    ret, frame = cap.read()
                except Exception as e:  # noqa: BLE001
                    ret = False
                    self._ultimo_error = str(e)
                if ret and frame is not None:
                    with self._lock:
                        self._frame = frame
                else:
                    # Frame caído (corte WiFi/RTSP): salir para reconectar
                    break
            cap.release()
            if not self._detener.is_set():
                time.sleep(0.5)

    def capturar(self) -> np.ndarray:
        with self._lock:
            frame = self._frame
        if frame is None:
            raise RuntimeError("Sin frames disponibles de la cámara")
        return frame

    def cerrar(self):
        self._detener.set()


class CapturadorMJPEG:
    """
    Capturador para streams HTTP MJPEG (formato de muchas cámaras IP
    económicas y de la app IP Webcam) con LATENCIA MÍNIMA.

    Por qué: OpenCV lee el stream MJPEG acumulando frames en un buffer
    interno, lo que produce retraso de segundos entre lo que ocurre y
    lo que se procesa. Este capturador:

      1. Un hilo en segundo plano consume el stream continuamente.
      2. Conserva SOLO el frame más reciente.
      3. capturar() devuelve ese frame al instante (sin esperar).

    Resultado: el cambio se detecta en milisegundos, no en segundos.
    """

    def __init__(self, fuente, nombre="camara", timeout_segundos=5):
        self.fuente = fuente
        self.nombre = nombre
        self.timeout = timeout_segundos
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._detener = threading.Event()
        self._ultimo_error = None

        self._hilo = threading.Thread(
            target=self._consumir_stream, daemon=True
        )
        self._hilo.start()

        # Esperar el primer frame (con límite de tiempo)
        for _ in range(max(timeout_segundos * 10, 20)):
            with self._lock:
                listo = self._frame is not None or self._ultimo_error is not None
            if listo:
                break
            time.sleep(0.1)
        if self._frame is None:
            self._detener.set()
            motivo = self._ultimo_error or "sin respuesta (timeout)"
            raise RuntimeError(
                f"No se pudo abrir la cámara MJPEG: {fuente} ({motivo})"
            )

    def _consumir_stream(self):
        """Hilo: lee el MJPEG y guarda siempre el frame más nuevo."""
        import urllib.request

        while not self._detener.is_set():
            try:
                req = urllib.request.Request(self.fuente)
                with urllib.request.urlopen(
                    req, timeout=self.timeout
                ) as resp:
                    contenido = bytearray()
                    while not self._detener.is_set():
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        contenido += chunk
                        self._extraer_frames(contenido)
            except Exception as e:  # noqa: BLE001 — intencional: reconexión
                self._ultimo_error = str(e)
            # Reconectar tras una pausa corta si se cortó el stream
            if not self._detener.is_set():
                time.sleep(1.0)

    def _extraer_frames(self, buffer: bytearray):
        """
        Extrae los JPEG del flujo MJPEG (delimitados por marcadores
        SOI 0xFFD8 y EOI 0xFFD9) y conserva solo el último decodificado.
        """
        import cv2

        inicio = buffer.find(b"\xff\xd8")
        fin = buffer.find(b"\xff\xd9", inicio + 2)
        while inicio != -1 and fin != -1:
            jpeg = bytes(buffer[inicio:fin + 2])
            del buffer[:fin + 2]
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                with self._lock:
                    self._frame = frame
            inicio = buffer.find(b"\xff\xd8")
            fin = buffer.find(b"\xff\xd9", inicio + 2)

    def capturar(self) -> np.ndarray:
        with self._lock:
            frame = self._frame
        if frame is None:
            raise RuntimeError("Sin frames disponibles de la cámara")
        return frame

    def cerrar(self):
        self._detener.set()


def crear_capturador(config) -> Union[
    "CapturadorPantalla", "CapturadorCamara", "CapturadorMJPEG"
]:
    """Factory: devuelve el capturador según la configuración."""
    if config.fuente == "camara":
        fuente = config.camara_fuente
        try:
            fuente_int = int(fuente)
        except ValueError:
            fuente_int = None

        if fuente_int is not None:
            return CapturadorCamara(fuente_int, nombre=config.nombre_camara)

        # Streams HTTP (MJPEG) → capturador de baja latencia
        if fuente.lower().startswith(("http://", "https://")):
            return CapturadorMJPEG(fuente, nombre=config.nombre_camara)

        # RTSP y otros → OpenCV con reconexión
        return CapturadorCamara(fuente, nombre=config.nombre_camara)

    return CapturadorPantalla(config.region, config.monitor)

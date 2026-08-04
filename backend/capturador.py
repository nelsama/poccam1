"""Capturadores de imagen: pantalla, cámara USB/IP y stream HTTP MJPEG."""

import threading
import time
from typing import Union

import numpy as np


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

    Incluye tolerancia a cortes del stream (común en WiFi):
    si un frame falla, reintenta hasta 3 veces antes de reportar error,
    y devuelve el último frame válido si la reconexión tiene éxito.
    """

    def __init__(self, fuente, nombre="camara", timeout_segundos=5):
        self.fuente = fuente
        self.nombre = nombre
        self.timeout = timeout_segundos
        self.ultimo_frame = None
        self.cap = self._abrir()

    def _abrir(self):
        import cv2
        # Backend correcto según el tipo de fuente:
        #  - URL RTSP → FFMPEG (maneja redes)
        #  - Índice numérico → DirectShow en Windows (cámaras USB)
        es_url = isinstance(self.fuente, str)
        if es_url:
            cap = cv2.VideoCapture(self.fuente, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout * 1000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout * 1000)
        else:
            cap = cv2.VideoCapture(self.fuente, cv2.CAP_DSHOW)
        # Buffer mínimo (1 frame): evita que OpenCV acumule frames viejos
        # del stream y devuelva video retrasado. Solo conserva el más reciente.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"No se pudo abrir la cámara: {self.fuente}")
        # Permitir que la cámara se estabilice (exposición, balance, etc.)
        for _ in range(5):
            ret, frame = cap.read()
            if ret:
                self.ultimo_frame = frame
        return cap

    def capturar(self) -> np.ndarray:
        for intento in range(3):
            ret, frame = self.cap.read()
            if ret and frame is not None:
                self.ultimo_frame = frame
                return frame
            # Frame caído (corte WiFi): intentar reconectar
            self.cap.release()
            self.cap = self._abrir()
        # Si todo falla, devolver el último frame válido en vez de morir
        if self.ultimo_frame is not None:
            return self.ultimo_frame
        raise RuntimeError(f"Sin conexión con la cámara: {self.fuente}")

    def cerrar(self):
        self.cap.release()


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
        for _ in range(timeout_segundos * 10):
            with self._lock:
                if self._frame is not None:
                    break
            time.sleep(0.1)
        if self._frame is None:
            raise RuntimeError(
                f"No se pudo abrir la cámara MJPEG: {fuente} "
                f"({self._ultimo_error})"
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

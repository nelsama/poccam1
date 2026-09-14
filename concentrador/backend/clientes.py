"""Cliente para comunicarse con los workers (la API de cada cámara).

Cada worker expone la API documentada en `API.md`. El concentrador la usa
para: leer su estado/config, aplicarle configuración y recibir sus eventos
y análisis (vía SSE).
"""

import json
import logging
import threading
import time

import urllib.error
import urllib.request

logger = logging.getLogger("concentrador")


class Worker:
    """Representa un worker remoto (una cámara)."""

    def __init__(self, worker_id: str, url: str, token: str = "",
                 timeout: int = 10):
        self.worker_id = worker_id
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ── HTTP base ──────────────────────────────────────────────────

    def _cabeceras(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, ruta: str, timeout: int | None = None):
        req = urllib.request.Request(
            f"{self.url}{ruta}", headers=self._cabeceras(), method="GET")
        with urllib.request.urlopen(
                req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, ruta: str, cuerpo: dict):
        datos = json.dumps(cuerpo).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}{ruta}", data=datos,
            headers=self._cabeceras(), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── Operaciones de alto nivel ──────────────────────────────────

    def estado(self) -> dict | None:
        """Estado completo del worker (config + runtime). None si falla."""
        try:
            return self._get("/api/estado-sistema")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning("Worker %s no responde: %s", self.worker_id, e)
            return None

    def vivo(self) -> bool:
        return self.estado() is not None

    def leer_config(self) -> dict | None:
        try:
            return self._get("/api/config")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning("No se pudo leer config de %s: %s",
                           self.worker_id, e)
            return None

    def aplicar_config(self, datos: dict) -> bool:
        """Aplica configuración al worker.

        Usa el endpoint EXTERNO: la cámara (`camara_fuente`/`fuente`) NO se
        cambia desde acá — es hardware local del worker.
        """
        try:
            self._post("/api/externo/config", datos)
            return True
        except urllib.error.HTTPError as e:
            logger.warning("Worker %s rechazó la config (%s): %s",
                           self.worker_id, e.code, e.read()[:200])
            return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning("No se pudo aplicar config a %s: %s",
                           self.worker_id, e)
            return False

    def analisis_recientes(self, limite: int = 10) -> list:
        """Últimos análisis guardados por el worker."""
        try:
            datos = self._get("/api/analisis")
            return datos.get("analisis", [])[:limite]
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning("No se pudieron leer análisis de %s: %s",
                           self.worker_id, e)
            return []

    # ── SSE: recibir análisis en vivo ──────────────────────────────

    def escuchar_analisis(self, al_recibir, detener: threading.Event):
        """Se suscribe al SSE `/api/eventos-analisis` del worker.

        Cada vez que el worker avisa que hay un análisis nuevo, llama a
        `al_recibir(worker_id)`. Reconecta automáticamente si se corta.
        Bloquea hasta que `detener` se active (correr en un hilo).
        """
        ruta = f"{self.url}/api/eventos-analisis"
        while not detener.is_set():
            try:
                req = urllib.request.Request(ruta, headers={
                    "Accept": "text/event-stream",
                    **({"Authorization": f"Bearer {self.token}"}
                       if self.token else {}),
                })
                with urllib.request.urlopen(
                        req, timeout=self.timeout) as resp:
                    logger.info("SSE conectado con %s", self.worker_id)
                    for linea in resp:
                        if detener.is_set():
                            break
                        texto = linea.decode("utf-8", "replace").strip()
                        if texto.startswith("data:"):
                            try:
                                al_recibir(self.worker_id)
                            except Exception:  # noqa: BLE001
                                logger.exception(
                                    "Error procesando aviso de %s",
                                    self.worker_id)
            except Exception as e:  # noqa: BLE001 — reconexión
                if not detener.is_set():
                    logger.warning("SSE de %s cortado: %s. Reintentando…",
                                   self.worker_id, e)
                    time.sleep(3.0)

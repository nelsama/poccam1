"""Concentrador: agrega varios workers (cámaras) y habla con el sistema
externo (nube).

Qué hace:
  1. Se conecta a cada worker (por su API documentada en `API.md`).
  2. Se suscribe a sus avisos SSE de análisis nuevos.
  3. Reenvía cada análisis al backend externo (sobre genérico del contrato).
  4. Baja la configuración de la nube y la reparte a cada worker.
  5. Si no hay internet, encola los análisis y los reintenta.

Uso:
    python concentrador/main.py
    python concentrador/main.py --config concentrador/config.yaml
"""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

# Permitir ejecutar desde la raíz del repo: `python concentrador/main.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from concentrador.backend.clientes import Worker  # noqa: E402
from concentrador.backend.cola import ColaPendientes  # noqa: E402
from concentrador.backend.nube import ClienteNube  # noqa: E402

logger = logging.getLogger("concentrador")

# Cada cuánto se reintenta enviar lo pendiente y bajar config
INTERVALO_REINTENTO = 30.0
INTERVALO_CONFIG = 60.0
INTERVALO_ESTADO = 120.0


class Concentrador:
    """Proceso principal del concentrador."""

    def __init__(self, ruta_config: str):
        self.config = self._cargar(ruta_config)
        self.detener = threading.Event()

        n = self.config.get("nube", {}) or {}
        self.nube = ClienteNube(
            base_url=n.get("base_url", ""),
            token=n.get("token", ""),
            concentrador_id=n.get("concentrador_id", "concentrador-1"),
        )
        self.nube_habilitada = bool(n.get("enabled", False))

        self.cola = ColaPendientes(
            Path(__file__).resolve().parent / "pendientes.jsonl")

        self.workers = {}
        for w in self.config.get("workers", []) or []:
            if not w.get("id") or not w.get("url"):
                logger.warning("Worker mal configurado, se ignora: %s", w)
                continue
            self.workers[w["id"]] = Worker(
                worker_id=w["id"], url=w["url"], token=w.get("token", ""))

        # Evitar procesar el mismo análisis dos veces
        self._procesados = set()
        self._lock = threading.Lock()
        self.conteo_enviados = 0

    # ── Configuración ──────────────────────────────────────────────

    def _cargar(self, ruta: str) -> dict:
        try:
            with open(ruta, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except OSError as e:
            logger.error("No se pudo leer %s: %s", ruta, e)
            sys.exit(1)

    # ── Recolección ────────────────────────────────────────────────

    def _al_recibir_aviso(self, worker_id: str):
        """Callback del SSE: el worker avisa que hay un análisis nuevo."""
        worker = self.workers.get(worker_id)
        if worker is None:
            return
        for analisis in worker.analisis_recientes(limite=10):
            evento_id = analisis.get("evento_id")
            if not evento_id:
                continue
            with self._lock:
                if evento_id in self._procesados:
                    continue
                self._procesados.add(evento_id)
            self._enviar_analisis(worker_id, analisis)

    def _enviar_analisis(self, worker_id: str, analisis: dict):
        """Envía un análisis al sistema externo (o lo encola si falla)."""
        sobre = dict(analisis)
        # Garantizar los campos del contrato
        sobre.setdefault("tipo", "analisis_ia")
        sobre["worker_id"] = worker_id

        if not self.nube_habilitada:
            logger.info("🔍 [nube deshabilitada] Análisis de %s: %s",
                        worker_id, analisis.get("datos"))
            return

        if self.nube.enviar_analisis(sobre):
            self.conteo_enviados += 1
            logger.info("📤 Análisis de %s enviado a la nube (%s)",
                        worker_id, sobre.get("evento_id"))
        else:
            logger.warning("⚠️ No se pudo enviar; encolado para reintento")
            self.cola.encolar(sobre)

    # ── Sincronización de configuración ────────────────────────────

    def _sincronizar_config(self):
        """Baja config de la nube y la reparte a los workers."""
        if not self.nube_habilitada:
            return
        datos = self.nube.bajar_config()
        if not datos:
            return
        for worker_id, bloques in (datos.get("workers") or {}).items():
            worker = self.workers.get(worker_id)
            if worker is None:
                logger.warning("Config para worker desconocido: %s", worker_id)
                continue
            # La cámara NO se cambia desde afuera (el worker lo ignora igual)
            if worker.aplicar_config(bloques):
                logger.info("⚙️ Config aplicada a %s", worker_id)

    def _reportar_estado(self):
        if not self.nube_habilitada:
            return
        estado = {}
        for wid, worker in self.workers.items():
            est = worker.estado()
            if est is None:
                estado[wid] = {"camara_viva": False}
                continue
            rt = est.get("runtime", {})
            estado[wid] = {
                "camara_viva": rt.get("camara_viva", False),
                "resolucion": rt.get("camara_resolucion"),
                "ia_activa": rt.get("ia_activa", False),
                "capturas": rt.get("capturas", 0),
                "eventos": rt.get("cambios", 0),
            }
        self.nube.reportar_estado(estado)

    # ── Hilos ──────────────────────────────────────────────────────

    def _hilo_sse(self, worker: Worker):
        worker.escuchar_analisis(self._al_recibir_aviso, self.detener)

    def _hilo_cola(self):
        while not self.detener.is_set():
            if self.nube_habilitada and self.cola.cantidad() > 0:
                self.cola.reenviar(self.nube.enviar_analisis)
            self.detener.wait(INTERVALO_REINTENTO)

    def _hilo_config(self):
        while not self.detener.is_set():
            try:
                self._sincronizar_config()
            except Exception:  # noqa: BLE001
                logger.exception("Error sincronizando configuración")
            self.detener.wait(INTERVALO_CONFIG)

    def _hilo_estado(self):
        while not self.detener.is_set():
            try:
                self._reportar_estado()
            except Exception:  # noqa: BLE001
                logger.exception("Error reportando estado")
            self.detener.wait(INTERVALO_ESTADO)

    # ── Arranque ───────────────────────────────────────────────────

    def describir(self):
        logger.info("═" * 50)
        logger.info("Concentrador iniciado")
        logger.info("Workers:  %d", len(self.workers))
        for wid, w in self.workers.items():
            logger.info("  • %-20s %s", wid, w.url)
        if self.nube_habilitada:
            logger.info("Nube:     %s", self.nube.base_url)
        else:
            logger.info("Nube:     DESHABILITADA (modo local)")
        if self.cola.cantidad():
            logger.info("Pendientes en cola: %d", self.cola.cantidad())
        logger.info("Ctrl+C para detener.")
        logger.info("═" * 50)

    def ejecutar(self):
        self.describir()

        # Un hilo SSE por worker
        for worker in self.workers.values():
            threading.Thread(target=self._hilo_sse, args=(worker,),
                             daemon=True).start()
        # Hilos de mantenimiento
        threading.Thread(target=self._hilo_cola, daemon=True).start()
        threading.Thread(target=self._hilo_config, daemon=True).start()
        threading.Thread(target=self._hilo_estado, daemon=True).start()

        try:
            while not self.detener.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Deteniendo…")
            self.detener.set()
            time.sleep(0.5)
            logger.info("Análisis enviados en esta sesión: %d",
                        self.conteo_enviados)
            logger.info("Pendientes en cola: %d", self.cola.cantidad())


def main():
    parser = argparse.ArgumentParser(
        description="Concentrador de workers (cámaras)")
    parser.add_argument("--config", "-c",
                        default=str(Path(__file__).resolve().parent /
                                    "config.yaml"),
                        help="Ruta al YAML de configuración")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    Concentrador(args.config).ejecutar()


if __name__ == "__main__":
    main()

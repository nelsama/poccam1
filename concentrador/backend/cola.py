"""Cola local de pendientes para el envío a la nube.

Si no hay internet (o el backend externo falla), los análisis se guardan en
disco y se reintentan después. Así no se pierde información.

Formato: un JSON por línea (JSONL), append-only. Al procesar, se reescribe
el archivo con lo que quedó pendiente.
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("concentrador")


class ColaPendientes:
    """Cola persistente en disco (FIFO) de sobres a enviar."""

    def __init__(self, ruta: str | Path, maximo: int = 10000):
        self.ruta = Path(ruta)
        self.maximo = maximo
        self._lock = threading.Lock()
        self.ruta.parent.mkdir(parents=True, exist_ok=True)

    # ── Escritura ──────────────────────────────────────────────────

    def encolar(self, sobre: dict):
        """Agrega un sobre a la cola de pendientes."""
        with self._lock:
            try:
                with open(self.ruta, "a", encoding="utf-8") as f:
                    f.write(json.dumps(sobre, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("No se pudo encolar el análisis: %s", e)

    def _leer_todos(self) -> list:
        if not self.ruta.exists():
            return []
        pendientes = []
        try:
            with open(self.ruta, encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        pendientes.append(json.loads(linea))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return pendientes

    # ── Procesamiento ──────────────────────────────────────────────

    def reenviar(self, enviar_fn) -> int:
        """Intenta enviar todos los pendientes con `enviar_fn(sobre)`.

        `enviar_fn` debe devolver True si se envió bien. Al primer fallo se
        detiene (para no insistir si no hay conexión) y deja el resto en la
        cola. Devuelve cuántos se enviaron.
        """
        with self._lock:
            pendientes = self._leer_todos()
            if not pendientes:
                return 0

            enviados = 0
            restantes = []
            for i, sobre in enumerate(pendientes):
                if restantes:
                    # Ya falló antes: conservar el resto sin intentar
                    restantes.append(sobre)
                    continue
                try:
                    ok = enviar_fn(sobre)
                except Exception:  # noqa: BLE001
                    logger.exception("Error reenviando un pendiente")
                    ok = False
                if ok:
                    enviados += 1
                else:
                    restantes.append(sobre)

            self._escribir(restantes)
            if enviados:
                logger.info("📤 Reenviados %d pendientes (%d quedan)",
                            enviados, len(restantes))
            return enviados

    def _escribir(self, sobres: list):
        try:
            if not sobres:
                # Sin pendientes: borrar el archivo para dejarlo limpio
                if self.ruta.exists():
                    self.ruta.unlink()
                return
            texto = "\n".join(
                json.dumps(s, ensure_ascii=False) for s in sobres) + "\n"
            self.ruta.write_text(texto, encoding="utf-8")
        except OSError as e:
            logger.warning("No se pudo actualizar la cola: %s", e)

    def cantidad(self) -> int:
        with self._lock:
            return len(self._leer_todos())

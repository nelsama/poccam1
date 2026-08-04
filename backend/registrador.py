"""Registro de eventos: log estructurado (JSONL) + guardado de imágenes."""

import json
import logging
from datetime import datetime
from pathlib import Path

from backend.config import Config
from backend.web import notificar_captura_nueva

logger = logging.getLogger("backend")


class RegistradorEventos:
    """
    Escribe cada evento de cambio en:
      1. eventos.jsonl  → log estructurado (una línea JSON por evento)
      2. capturas_cambio/ → la imagen del evento (+ versión marcada)
    """

    def __init__(self, config: Config):
        self.config = config
        self.ruta_log = Path(config.log_eventos)
        self.output_dir = Path(config.output_dir)
        self.max_imagenes = config.max_imagenes

        if config.save_changes:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            # Limpiar archivos acumulados de ejecuciones anteriores
            self._limitar_imagenes()

    def registrar(self, evento: dict) -> str:
        """
        Persiste un evento de cambio. `evento` contiene al menos:
            score, area_px, camara, imagen (np.ndarray), imagen_marcada (opcional)
        Retorna el id del evento (timestamp).
        """
        evento_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]

        # Guardar imágenes
        ruta_original = ""
        ruta_marcada = ""
        if self.config.save_changes:
            ruta_original = str(self.output_dir / f"evento_{evento_id}_original.png")
            import cv2
            cv2.imwrite(ruta_original, evento["imagen"])

            if evento.get("imagen_marcada") is not None:
                ruta_marcada = str(self.output_dir / f"evento_{evento_id}_marcado.png")
                cv2.imwrite(ruta_marcada, evento["imagen_marcada"])

            self._limitar_imagenes()

        # Escribir línea JSON en el log de eventos
        registro = {
            "evento_id": evento_id,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "camara": evento.get("camara", "desconocida"),
            "metodo": evento.get("metodo", ""),
            "score": round(float(evento.get("score", 0.0)), 6),
            "area_px": int(evento.get("area_px", 0)),
            "area_borde": int(evento.get("area_borde", 0)),
            "imagen_original": ruta_original,
            "imagen_marcada": ruta_marcada,
            "capturas_total": evento.get("capturas_total", 0),
        }

        with open(self.ruta_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

        logger.info(
            f"📝 EVENTO REGISTRADO #{registro['capturas_total']} | "
            f"score={registro['score']:.4f} | "
            f"imagen={ruta_original}"
        )

        # Avisar al panel web que hay una captura nueva (solo si hay
        # imágenes guardadas, para no disparar notificaciones vacías)
        if ruta_original:
            notificar_captura_nueva()

        return evento_id

    def _limitar_imagenes(self):
        """
        Mantiene como máximo `max_imagenes` archivos en la carpeta de
        capturas. Cada evento genera 2 archivos (original + marcado),
        así que un límite de 20 imágenes = ~10 eventos recientes.
        Elimina los más antiguos (ordenados por nombre = por timestamp).
        """
        if self.max_imagenes <= 0:
            return  # 0 o negativo = sin límite

        # Nota: el patrón busca SOLO archivos de eventos (evento_*.png).
        # Otros archivos en la carpeta no se tocan.
        imagenes = sorted(self.output_dir.glob("evento_*.png"))
        exceso = len(imagenes) - self.max_imagenes
        if exceso <= 0:
            return

        for antiguo in imagenes[:exceso]:
            try:
                antiguo.unlink()
                logger.debug(f"🗑️ Eliminada imagen antigua: {antiguo.name}")
            except OSError as e:
                logger.warning(f"No se pudo eliminar {antiguo.name}: {e}")

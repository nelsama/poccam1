"""Registro de eventos: log estructurado (JSONL) + guardado de imágenes."""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from backend.config import Config
from backend.web import notificar_captura_nueva, notificar_analisis_nuevo

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
        # Carpeta donde se guardan los análisis JSON de la IA
        self.analisis_dir = Path(config.output_dir).parent / "analisis_ia"

        if config.save_changes:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            # Limpiar archivos acumulados de ejecuciones anteriores
            self._limitar_imagenes()
        self.analisis_dir.mkdir(parents=True, exist_ok=True)

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
            "worker_id": self.config.worker_id_efectivo(),
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
        self._analizar_si_ia(ruta_original, evento_id, evento)

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

    # ── Análisis por IA (DeepSeek Vision) ──────────────────────────

    def _analizar_si_ia(self, ruta_original: str, evento_id: str, evento: dict):
        """Si la IA está habilitada y hay imagen, la analiza en un hilo
        aparte (no bloquea el bucle del monitor) y guarda el JSON con la
        misma estructura fija del análisis, además de notificar."""
        if not self.config.ia_enabled:
            return
        if not ruta_original:
            return

        def trabajo():
            from backend.ia import analizar_imagen
            try:
                resultado = analizar_imagen(
                    ruta_original,
                    api_key=self.config.ia_api_key or None,
                    model=self.config.ia_model,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Fallo el análisis IA {evento_id}: {e}")
                return

            # Registrar en el log el resultado (resumen, cualquiera sea
            # la estructura que devuelva la IA)
            def _acotar(v):
                s = str(v)
                return s if len(s) < 40 else s[:37] + "..."
            campos = [f"{k}={_acotar(v)}" for k, v in list(resultado.items())[:8]]
            logger.info("🕵️ Análisis IA: " + ", ".join(campos))

            # Guardar el análisis con el SOBRE GENÉRICO del contrato:
            # los metadatos del sistema quedan en el nivel superior y la
            # respuesta libre de la IA va anidada en `datos` (así el
            # sistema externo puede guardarla como JSON opaco sin atarse
            # a un esquema).
            analisis = {
                "tipo": "analisis_ia",
                "worker_id": self.config.worker_id_efectivo(),
                "evento_id": evento_id,
                "timestamp": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"),
                "esquema": self.config.ia_esquema,
                "datos": resultado,
                "meta": {
                    "modelo": self.config.ia_model,
                    "detail": self.config.ia_detail,
                    "area_px": int(evento.get("area_px", 0)),
                    "area_borde": int(evento.get("area_borde", 0)),
                    "score": float(evento.get("score", 0.0)),
                    "imagen_original": ruta_original,
                    "capturas_total": evento.get("capturas_total", 0),
                },
            }
            nombre = f"analisis_{evento_id}.json"
            ruta_analisis = self.analisis_dir / nombre
            try:
                ruta_analisis.write_text(
                    json.dumps(analisis, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning(f"No se pudo guardar el análisis: {e}")
                return

            # Avisar al panel web
            notificar_analisis_nuevo(str(ruta_analisis))
            self._limitar_analisis()

        hilo = threading.Thread(target=trabajo, daemon=True)
        hilo.start()

    def _limitar_analisis(self, maximo=10):
        """Mantiene como mucho `maximo` archivos de análisis en la carpeta
        analisis_ia; borra los más antiguos (nombre = timestamp desc)."""
        if maximo <= 0:
            return
        archivos = sorted(self.analisis_dir.glob("analisis_*.json"))
        exceso = len(archivos) - maximo
        if exceso <= 0:
            return
        for antiguo in archivos[:exceso]:
            try:
                antiguo.unlink()
                logger.debug(f"🗑️ Análisis antiguo eliminado: {antiguo.name}")
            except OSError as e:
                logger.warning(f"No se pudo eliminar {antiguo.name}: {e}")

    def ultimos_analisis(self, cantidad=1) -> list:
        """Devuelve los últimos análisis JSON (ordenados por nombre =
        timestamp), más recientes primero."""
        if not self.analisis_dir.exists():
            return []
        archivos = sorted(self.analisis_dir.glob("analisis_*.json"),
                          reverse=True)
        lista = []
        for archivo in archivos[:cantidad]:
            try:
                lista.append(json.loads(archivo.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return lista

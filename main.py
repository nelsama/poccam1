"""Detector de Cambios para Paneles Industriales

Backend que:
  1. Pide una imagen a la cámara a intervalos configurables.
  2. Compara con la anterior (SSIM por defecto).
  3. Solo cuando detecta un cambio: guarda la imagen y registra
     el evento en eventos.jsonl (log estructurado).

Uso:
    python main.py                    # usa config.yaml
    python main.py --intervalo 0.5    # parametrizar el sondeo
    python main.py --camara 0         # cámara USB
    python main.py --camara "rtsp://..."  # cámara IP
"""

import argparse
import logging
import sys
import threading
import time

from backend.capturador import crear_capturador, rotar_frame
from backend.config import Config
from backend.detector import DetectorCambios
from backend.registrador import RegistradorEventos

logger = logging.getLogger("main")

# Segundos de espera entre reintentos de conexión con la cámara
# (si está apagada, sin red o el stream aún no arrancó).
REINTENTO_SEGUNDOS = 5.0


class MonitorBackend:
    """Bucle principal: capturar → detectar → registrar (si hay cambio)."""

    def __init__(self, config: Config):
        self.config = config
        self._configurar_logging()

        self.capturador = self._crear_capturador()
        self.detector = DetectorCambios(
            metodo=config.metodo,
            umbral=config.umbral,
            min_area_px=config.min_area_px,
            blur_ksize=config.blur_ksize,
            marcar_cambios=config.marcar_cambios,
            frames_estables=config.frames_estables,
            alinear_imagenes=config.alinear_imagenes,
            max_desplazamiento=config.max_desplazamiento,
        )
        # El análisis IA arranca SIEMPRE DISPONIBLE pero DETENIDO:
        # nunca se auto-activa al iniciar (para no gastar). El botón del
        # panel lo activa por sesión. Esto sobreescribe el YAML.
        self.config.ia_enabled = False

        self.registrador = RegistradorEventos(config)

        # Detectar la resolución real del stream y aplicar el preset
        # correspondiente (uno de PRESETS_CAMARA), si está habilitado.
        self._detectar_resolucion_y_preset()

        self._iniciar_web()
        self._describir_configuracion()

        self.conteo_capturas = 0
        self.conteo_cambios = 0
        self._ultimo_evento = 0.0

    def _crear_capturador(self):
        """Crea el capturador con reintento.

        Si la cámara no responde (apagada, sin red, stream no iniciado),
        lo reintenta cada REINTENTO_SEGUNDOS en vez de terminar con un
        traceback: así el backend queda esperando y arranca solo cuando
        la cámara vuelve a estar disponible.
        """
        while True:
            try:
                return crear_capturador(self.config)
            except RuntimeError as e:
                logger.error(
                    "❌ No se pudo abrir la fuente de imágenes: %s", e
                )
                logger.info(
                    f"🔄 Reintentando en {REINTENTO_SEGUNDOS:.0f} s... "
                    "(Ctrl+C para salir)"
                )
                try:
                    time.sleep(REINTENTO_SEGUNDOS)
                except KeyboardInterrupt:
                    logger.info("Abortado por el usuario.")
                    sys.exit(1)

    def _detectar_resolucion_y_preset(self):
        """Lee un frame del stream, detecta su resolución y aplica el
        preset del panel web que le corresponde (exacto o el más
        cercano). Solo aplica con fuente = cámara."""
        if self.config.fuente != "camara":
            return
        from backend.web import buscar_preset

        try:
            frame = self.capturador.capturar()
            alto, ancho = frame.shape[:2]
        except Exception:
            logger.warning(
                "No se pudo leer un frame para detectar la resolución"
            )
            return

        self.config.camara_resolucion = (ancho, alto)
        preset = buscar_preset(ancho, alto)
        if preset is None:
            logger.info(f"📷 Resolución detectada: {ancho}x{alto} — sin preset")
            return

        self.config.preset_aplicado = preset["id"]
        if self.config.aplicar_preset_al_iniciar:
            # Aplicar el preset (valores de partida para esa resolución)
            self.detector.actualizar(preset["deteccion"])
            for clave, valor in preset["deteccion"].items():
                setattr(self.config, clave, valor)
            try:
                self.config.guardar()
            except OSError as e:
                logger.warning(f"No se pudo persistir el preset: {e}")
            logger.info(f"📷 Resolución detectada: {ancho}x{alto} → "
                        f"preset '{preset['nombre']}' aplicado")
        else:
            logger.info(f"📷 Resolución detectada: {ancho}x{alto} → "
                        f"preset '{preset['nombre']}' sugerido "
                        "(aplicar_preset_al_iniciar: false)")

    def _iniciar_web(self):
        """Arranca el panel web (configuración del área de análisis)."""
        if not self.config.web_enabled:
            return
        from backend.web import crear_app
        app = crear_app(self.capturador, config=self.config, detector=self.detector)
        hilo = threading.Thread(
            target=app.run,
            kwargs={"host": self.config.web_host,
                    "port": self.config.web_port,
                    "debug": False,
                    "use_reloader": False},
            daemon=True,
        )
        hilo.start()
        self._url_web = f"http://localhost:{self.config.web_port}"
        logger.info(f"🌐 Panel web:      {self._url_web}")

    def _configurar_logging(self):
        nivel = getattr(logging, self.config.nivel_log.upper(), logging.INFO)
        logging.basicConfig(
            level=nivel,
            format="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )

    def _describir_configuracion(self):
        if self.config.fuente == "camara":
            detalle = f"cámara '{self.config.nombre_camara}' ({self.config.camara_fuente})"
        else:
            detalle = f"pantalla (monitor {self.config.monitor})"
        if self.config.region:
            detalle += f" región {self.config.region}"

        logger.info("══════════════════════════════════════════")
        logger.info("Backend de monitoreo de paneles iniciado")
        logger.info(f"Fuente:       {detalle}")
        logger.info(f"Sondeo:       cada {self.config.intervalo_segundos}s")
        logger.info(f"Detector:     {self.config.metodo} | umbral={self.config.umbral}")
        logger.info(f"Log eventos:  {self.config.log_eventos}")
        logger.info(f"Imágenes:     {self.config.output_dir}")
        logger.info("══════════════════════════════════════════")
        if self.config.web_enabled:
            logger.info(f"Área de análisis: define la ROI en {self._url_web}")
        logger.info("Ctrl+C para detener.")
        logger.info("══════════════════════════════════════════")

    def ejecutar(self):
        primera = True

        try:
            while True:
                inicio = time.monotonic()

                # 1. PEDIR imagen a la cámara
                imagen = self.capturador.capturar()
                self.conteo_capturas += 1
                # Aplicar rotación configurada (0/90/180/270)
                if self.config.rotacion:
                    imagen = rotar_frame(imagen, self.config.rotacion)

                # 2. Comparar contra la anterior
                resultado = self.detector.procesar(imagen)

                if primera:
                    alto, ancho = imagen.shape[:2]
                    logger.info(f"Primera captura lista ({ancho}x{alto}) — "
                                f"desde ahora se comparan imágenes")
                    primera = False
                    self._esperar(inicio)
                    continue

                # 3. ¿Hubo cambio? → registrar
                if resultado["hubo_cambio"]:
                    # Freno anti-spam: ignora eventos que ocurren muy seguido
                    # (ej: un LED que parpadea cada segundo). Solo registra
                    # si pasó el tiempo mínimo desde el último evento.
                    ahora = time.monotonic()
                    if ahora - self._ultimo_evento >= \
                            self.config.min_intervalo_eventos:
                        self._ultimo_evento = ahora
                        self.conteo_cambios += 1
                        evento = {
                            # Guardar SOLO el área analizada (ROI si está
                            # definido) — así la imagen refleja exactamente
                            # lo que disparó el detector.
                            "imagen": resultado["imagen_analizada"],
                            "imagen_marcada": resultado["imagen_marcada"],
                            "score": resultado["score"],
                            "area_px": resultado["area_px"],
                            "area_borde": resultado.get("area_borde", 0),
                            "metodo": self.config.metodo,
                            "camara": self._nombre_fuente(),
                            "capturas_total": self.conteo_capturas,
                        }
                        self.registrador.registrar(evento)
                    else:
                        logger.debug(
                            "Cambio ignorado (freno anti-spam activo)"
                        )
                else:
                    logger.debug(
                        f"Sin cambio (score={resultado['score']:.4f}) "
                        f"[captura {self.conteo_capturas}]"
                    )

                # 4. Estadísticas periódicas
                if self.conteo_capturas % 100 == 0:
                    tasa = self.conteo_cambios / self.conteo_capturas * 100
                    logger.info(
                        f"📊 {self.conteo_capturas} capturas | "
                        f"{self.conteo_cambios} cambios ({tasa:.1f}%)"
                    )

                self._esperar(inicio)

        except KeyboardInterrupt:
            self._resumen()
        except Exception:
            logger.exception("Error en el bucle de monitoreo")
            self._resumen()
            sys.exit(1)

    def _nombre_fuente(self) -> str:
        return self.config.nombre_camara

    def _esperar(self, inicio: float):
        """Espera el tiempo restante para respetar el intervalo configurado."""
        restante = self.config.intervalo_segundos - (time.monotonic() - inicio)
        if restante > 0:
            time.sleep(restante)

    def _resumen(self):
        logger.info("══════════════════ RESUMEN ══════════════════")
        logger.info(f"Total capturas: {self.conteo_capturas}")
        logger.info(f"Eventos registrados: {self.conteo_cambios}")
        if self.conteo_capturas > 0:
            logger.info(
                f"Tasa de cambios: "
                f"{self.conteo_cambios / self.conteo_capturas * 100:.2f}%"
            )
        logger.info(f"Log: {self.config.log_eventos}")
        logger.info("═════════════════════════════════════════════")
        self.capturador.cerrar()


def main():
    parser = argparse.ArgumentParser(
        description="Backend detector de cambios en paneles industriales"
    )
    parser.add_argument("--config", "-c", default="config.yaml",
                        help="Ruta al YAML de configuración")
    parser.add_argument("--intervalo", "-i", type=float, default=None,
                        help="Sobreescribe el intervalo de sondeo (segundos)")
    parser.add_argument("--camara", "-k", default=None,
                        help="Sobreescribe la fuente: índice USB o URL RTSP")
    parser.add_argument("--umbral", "-u", type=float, default=None,
                        help="Sobreescribe el umbral de detección (0-1)")
    parser.add_argument("--region", "-r", type=int, nargs=4, default=None,
                        help="Región de pantalla: left top width height")
    parser.add_argument("--metodo", "-m", default=None,
                        choices=["ssim", "diff", "mse"],
                        help="Método de detección")
    parser.add_argument("--marcar", action="store_true",
                        help="Dibujar contornos rojos sobre los cambios")
    args = parser.parse_args()

    config = Config.from_yaml(args.config)

    if args.intervalo is not None:
        config.intervalo_segundos = args.intervalo
    if args.umbral is not None:
        config.umbral = args.umbral
    if args.region is not None:
        config.region = list(args.region)
    if args.metodo is not None:
        config.metodo = args.metodo
    if args.marcar:
        config.marcar_cambios = True
    if args.camara is not None:
        config.fuente = "camara"
        config.camara_fuente = args.camara

    monitor = MonitorBackend(config)
    monitor.ejecutar()


if __name__ == "__main__":
    main()

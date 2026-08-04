"""Detección de cambios entre dos imágenes consecutivas.

Diseñado para paneles industriales con cámara FIJA:
  - Dispara por ÁREA ABSOLUTA de píxeles cambiados (un dígito que cambia
    mueve ~100-500 px, que es poco como fracción pero mucho como área).
  - El desenfoque elimina ruido del sensor/compresión.
  - La máscara marca solo las regiones que realmente cambiaron.
"""

import logging

import numpy as np

from backend.web import cargar_roi

logger = logging.getLogger("backend")


class DetectorCambios:
    """
    Compara la imagen actual contra una REFERENCIA ESTABLE y decide si
    hubo un cambio significativo. Métodos: diff, ssim, mse.

    Genérico para cualquier tipo de display (7 segmentos, matrices,
    LCD de alta resolución, colores variados, fondos distintos):
      - Filtro de estabilidad: el cambio debe persistir `frames_estables`
        capturas consecutivas para registrarse. Un parpadeo o ruido
        momentáneo desaparece rápido y NO llega a confirmarse.
      - Se compara contra la última imagen estable (no contra el frame
        anterior), así un elemento que parpadea ON/OFF no genera
        cambios en cada ciclo.
    """

    def __init__(self, metodo="diff", umbral=0.02, min_area_px=100,
                 blur_ksize=5, marcar_cambios=False, frames_estables=2,
                 alinear_imagenes=False, max_desplazamiento=10.0):
        self.metodo = metodo
        self.umbral = umbral
        self.min_area_px = min_area_px
        self.blur_ksize = blur_ksize
        self.marcar_cambios = marcar_cambios
        self.frames_estables = max(1, frames_estables)
        self.alinear_imagenes = alinear_imagenes
        self.max_desplazamiento = max(1.0, max_desplazamiento)
        # Referencia estable: la última imagen confirmada sin cambio
        self.imagen_referencia: np.ndarray | None = None
        # Contador de cambios consecutivos respecto a la referencia
        self.conteo_cambios = 0
        # Último desplazamiento estimado (para diagnóstico)
        self.ultimo_desplazamiento: tuple[float, float] | None = None

    def procesar(self, imagen: np.ndarray):
        """
        Procesa una imagen y retorna un dict con el resultado:

            {
                "hubo_cambio": bool,   # True SOLO si el cambio se confirmó
                "score": float,
                "area_px": int,
                "imagen_marcada": np.ndarray | None,
                "imagen_analizada": np.ndarray
            }
        """
        # Aplicar ROI (área definida en el panel web): solo se analiza
        # esa región; el fondo queda ignorado por completo.
        roi = cargar_roi()
        imagen_analizada = imagen
        if roi:
            x, y, w, h = roi
            alto, ancho = imagen.shape[:2]
            if 0 <= x < ancho and 0 <= y < alto and w > 0 and h > 0 \
                    and x + w <= ancho and y + h <= alto:
                imagen_analizada = imagen[y:y + h, x:x + w]
            else:
                logger.warning(
                    f"ROI {roi} fuera de los límites del frame "
                    f"({ancho}x{alto}) — analizando imagen completa"
                )

        # Primera captura o referencia perdida → establecerla
        if self.imagen_referencia is None:
            self.imagen_referencia = imagen_analizada
            self.conteo_cambios = 0
            return {"hubo_cambio": False, "score": 0.0, "area_px": 0,
                    "imagen_marcada": None, "imagen_analizada": imagen_analizada}

        # Si el tamaño cambió (ROI definido/borrado) → nueva referencia
        if self.imagen_referencia.shape != imagen_analizada.shape:
            logger.debug("Tamaño de imagen cambiado (ROI modificado) — "
                         "reiniciando referencia")
            self.imagen_referencia = imagen_analizada
            self.conteo_cambios = 0
            return {"hubo_cambio": False, "score": 0.0, "area_px": 0,
                    "imagen_marcada": None, "imagen_analizada": imagen_analizada}

        # Preprocesar: desenfoque en COLOR (se conservan los 3 canales
        # RGB para no perder información de color del display)
        color_actual = self._a_color(imagen_analizada)
        color_referencia = self._a_color(self.imagen_referencia)

        # Compensación de vibración: alinear el frame actual contra la
        # referencia ANTES de comparar. La correlación de fase estima el
        # desplazamiento en X e Y (sub-píxel) y lo corrige.
        if self.alinear_imagenes:
            color_actual = self._alinear(color_referencia, color_actual)

        if self.metodo == "diff":
            resultado = self._diff(color_referencia, color_actual, imagen_analizada)
        elif self.metodo == "ssim":
            resultado = self._ssim(color_referencia, color_actual, imagen_analizada)
        elif self.metodo == "mse":
            resultado = self._mse(color_referencia, color_actual)
        else:
            raise ValueError(f"Método desconocido: {self.metodo}")

        # ── Filtro de estabilidad ──────────────────────────────────
        if resultado["hubo_cambio"]:
            # Cambio detectado respecto a la referencia estable
            self.conteo_cambios += 1
            if self.conteo_cambios >= self.frames_estables:
                # Cambio CONFIRMADO: persiste varias capturas
                # Actualizar la referencia al nuevo estado estable
                self.imagen_referencia = imagen_analizada
                self.conteo_cambios = 0
                resultado["hubo_cambio"] = True
                resultado["imagen_analizada"] = imagen_analizada
                return resultado
            # Aún no confirmado: NO actualizar la referencia (para que
            # el siguiente frame siga comparando contra la base)
            resultado["hubo_cambio"] = False
            resultado["imagen_analizada"] = imagen_analizada
            return resultado
        else:
            # Sin cambio respecto a la referencia → estado estable
            self.conteo_cambios = 0
            self.imagen_referencia = imagen_analizada
            resultado["imagen_analizada"] = imagen_analizada
            return resultado

    # ─── Internos ─────────────────────────────────────────────

    def _a_color(self, img: np.ndarray) -> np.ndarray:
        """
        Desenfoca la imagen CONSERVANDO el color (3 canales).
        No se convierte a gris porque eso pierde información importante
        para displays de colores (un LED rojo sobre fondo oscuro puede
        tener el mismo valor de gris que el fondo).
        """
        import cv2
        if self.blur_ksize > 0:
            k = self.blur_ksize if self.blur_ksize % 2 == 1 else self.blur_ksize + 1
            return cv2.GaussianBlur(img, (k, k), 0)
        return img

    def _alinear(self, referencia, actual):
        """
        Compensa la vibración de la cámara: estima el desplazamiento
        (X e Y, con sub-píxel) entre la referencia y el frame actual
        usando correlación de fase, y desplaza el frame para que
        coincida con la referencia. Así la vibración no aparece como
        un "cambio" falso en los bordes del display.
        """
        import cv2
        from skimage.registration import phase_cross_correlation
        from scipy.ndimage import shift

        # Correlación de fase sobre la imagen en gris (más estable)
        ref_gris = cv2.cvtColor(referencia, cv2.COLOR_BGR2GRAY)
        act_gris = cv2.cvtColor(actual, cv2.COLOR_BGR2GRAY)

        try:
            desplazamiento, error, _ = phase_cross_correlation(
                ref_gris, act_gris, upsample_factor=10
            )
        except Exception:
            # Si falla (imagen sin textura), devolver sin alinear
            self.ultimo_desplazamiento = None
            return actual

        # Límite de seguridad: solo corregir si el desplazamiento es
        # razonable (si es enorme, probablemente cambió la escena)
        dy, dx = float(desplazamiento[0]), float(desplazamiento[1])
        magnitud = (dx * dx + dy * dy) ** 0.5
        if magnitud > self.max_desplazamiento:
            self.ultimo_desplazamiento = (dy, dx)
            return actual  # desplazamiento demasiado grande, no corregir

        self.ultimo_desplazamiento = (dy, dx)
        # Imagen BGR de 3 canales → el desplazamiento debe tener 3 ejes;
        # el canal (eje 2) nunca se desplaza.
        return shift(actual, (dy, dx, 0), mode="nearest")

    def _decidir(self, area: int, total: int) -> bool:
        """
        Regla de decisión principal: ÁREA ABSOLUTA de píxeles cambiados.

        - `area > min_area_px` → hubo cambio estructural real (un dígito,
          un LED, una aguja). Es la condición dominante.
        - Guarda: si el cambio abarca ~todo el frame (>95%), probablemente
          la cámara se movió o se tapó → también es un evento, se reporta.
        """
        return area > self.min_area_px

    def _diff(self, anterior, actual, img_color):
        """
        Diferencia absoluta EN COLOR (3 canales). Se cuenta un píxel
        como cambiado si CUALQUIER canal difiere del umbral.
        """
        import cv2

        diff = cv2.absdiff(anterior, actual)  # 3 canales
        # Un píxel cambió si algún canal supera el umbral
        mascara = (np.max(diff, axis=2) > 25).astype(np.uint8) * 255
        # Limpiar manchas de ruido sueltas (apertura morfológica)
        mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        area = cv2.countNonZero(mascara)
        total = anterior.shape[0] * anterior.shape[1]

        hubo = self._decidir(area, total)
        score = area / total

        visual = None
        if hubo and self.marcar_cambios:
            visual = img_color.copy()
            self._marcar_cambios(visual, mascara)

        return {"hubo_cambio": hubo, "score": float(score),
                "area_px": int(area), "imagen_marcada": visual}

    def _ssim(self, anterior, actual, img_color):
        """
        SSIM EN COLOR: compara cada canal por separado y combina.
        Conserva la información de color (un LED rojo que cambia se
        detecta aunque su gris sea similar al fondo).
        """
        import cv2
        from skimage.metrics import structural_similarity as ssim

        # SSIM por canal (channel_axis=2 para BGR), combinar el mapa
        resultado = ssim(anterior, actual, full=True, data_range=255,
                         channel_axis=2)
        score = float(resultado[0])
        # El mapa de SSIM vale 1.0 donde las imágenes son IGUALES y
        # baja donde difieren. Se invierte y se promedia entre canales.
        diff_map = 1.0 - np.asarray(resultado[1], dtype=np.float64)
        diff_map = np.mean(diff_map, axis=2)  # promedio de los 3 canales
        diff = (diff_map * 255).astype(np.uint8)

        _, mascara = cv2.threshold(diff, 128, 255, cv2.THRESH_BINARY)
        mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        area = cv2.countNonZero(mascara)
        total = anterior.shape[0] * anterior.shape[1]

        hubo = self._decidir(area, total)

        visual = None
        if hubo and self.marcar_cambios:
            visual = img_color.copy()
            self._marcar_cambios(visual, mascara)

        return {"hubo_cambio": hubo, "score": float(1 - score),
                "area_px": int(area), "imagen_marcada": visual}

    def _mse(self, anterior, actual):
        mse = float(np.mean((anterior.astype(float) - actual.astype(float)) ** 2))
        score = min(mse / 65025.0, 1.0)  # 255² ≈ 65025 → normaliza a 0-1
        return {"hubo_cambio": score > self.umbral, "score": score,
                "area_px": 0, "imagen_marcada": None}

    def _marcar_cambios(self, visual, mascara):
        """
        Dibuja contornos rojos SOLO sobre cambios con área real.
        Filtra contornos diminutos (ruido de compresión/cámara).
        """
        import cv2
        contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
        relevantes = [c for c in contornos
                      if cv2.contourArea(c) >= max(self.min_area_px, 30)]
        cv2.drawContours(visual, relevantes, -1, (0, 0, 255), 2)

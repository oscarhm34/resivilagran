"""Comparacion de imagenes para identificar objetos perdidos.

Dos descriptores por foto, porque ninguno de los dos vale solo:

- **Embedding** (DINOv2-small en ONNX): reconoce la forma y la textura, y aguanta
  bien el cambio de angulo y de fondo. Su punto ciego es el color: medido sobre
  prendas de prueba, una camisa de cuadros AZUL puntuaba 0.97 contra una camisa
  de cuadros ROJA, por encima de 0.83 de la misma camisa roja fotografiada en
  otro sitio. Usado solo, mandaria a la trabajadora a la habitacion equivocada.

- **Histograma de color** (tono x saturacion): para ropa es la senal mas
  discriminante. No entra el brillo a proposito. Con brillo dentro, la misma
  prenda bajo otra luz daba 0.20 de parecido consigo misma; sin el, 0.91.

Las dos se suman con COLOR_WEIGHT. Todos los umbrales de aqui son provisionales:
estan ajustados sobre imagenes sinteticas y hay que recalibrarlos con fotos
reales de la residencia. Ver `flask calibrar-identificacion`.

El modulo no importa Flask a proposito: asi se puede probar sin contexto de
aplicacion y la parte de puntuacion se testea sin el modelo.
"""
from __future__ import annotations

import os
import threading

import numpy as np
from PIL import Image

# ── Parametros del descriptor ────────────────────────────────────────────────
# Cambiar cualquiera de estos invalida los descriptores ya guardados: subir
# entonces DESCRIPTOR_VERSION para que el backfill los recalcule.
DESCRIPTOR_VERSION = 'dinov2s-q-hsv18x3-v1'

CROP_FRAC = 0.60          # el fondo (cama, mesa, suelo) es ruido: se recorta
INPUT_SIZE = 224
HUE_BINS, SAT_BINS = 18, 3
CHROMA_MIN = 0.20         # por debajo de esta saturacion el pixel es gris
EMBED_DIM = 768           # CLS (384) + media de los parches (384)
HIST_DIM = HUE_BINS * SAT_BINS + 2

_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# ── Parametros de puntuacion ─────────────────────────────────────────────────
COLOR_WEIGHT = 0.35       # medido: acierta entre 0.3 y 0.5; a 0.6 ya falla
BAND_HIGH = 0.85
BAND_MEDIUM = 0.70
SCORE_FLOOR = 0.55        # por debajo no se ensena: es ruido
MAX_RESULTS = 8

BAND_LABELS = {'alta': 'Coincidencia alta',
               'media': 'Coincidencia media',
               'baja': 'Coincidencia baja'}

_DEFAULT_MODEL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'models', 'dinov2-small-q.onnx')

_session = None
_session_lock = threading.Lock()
_session_failed = False


def model_path() -> str:
    return os.environ.get('BELONGING_MODEL_PATH') or _DEFAULT_MODEL


def model_available() -> bool:
    """Si no esta el fichero del modelo, la identificacion se apaga entera.

    Se prefiere apagarla a dejarla funcionando solo con el color: unos
    resultados calladamente peores son peor que no tener el boton.
    """
    return os.path.exists(model_path())


def _load_session():
    """Carga perezosa: el modelo solo entra en memoria si alguien identifica."""
    global _session, _session_failed
    if _session is not None or _session_failed:
        return _session
    with _session_lock:
        if _session is not None or _session_failed:
            return _session
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1      # 2 workers x 2 hilos en el NAS
            _session = ort.InferenceSession(model_path(),
                                            sess_options=opts,
                                            providers=['CPUExecutionProvider'])
        except Exception:                      # noqa: BLE001 - el motivo lo loguea quien llama
            _session_failed = True
            _session = None
    return _session


def _center_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    cw, ch = max(1, int(w * CROP_FRAC)), max(1, int(h * CROP_FRAC))
    return img.crop(((w - cw) // 2, (h - ch) // 2, (w - cw) // 2 + cw, (h - ch) // 2 + ch))


def color_histogram(img: Image.Image) -> np.ndarray:
    """Tono x saturacion, ponderado por saturacion, sin brillo.

    Los pixeles poco saturados no tienen tono fiable, asi que van a dos casillas
    aparte (claro y oscuro) en vez de ensuciar el histograma: eso es lo que
    separa un jersey blanco de uno negro.
    """
    im = _center_crop(img.convert('RGB')).resize((128, 128), Image.BICUBIC).convert('HSV')
    a = np.asarray(im, dtype=np.float32) / 255.0
    h, s, v = a[..., 0].ravel(), a[..., 1].ravel(), a[..., 2].ravel()

    out = np.zeros(HIST_DIM, dtype=np.float32)
    croma = s > CHROMA_MIN
    if croma.any():
        hi = np.minimum((h[croma] * HUE_BINS).astype(np.int32), HUE_BINS - 1)
        si = np.minimum((s[croma] * SAT_BINS).astype(np.int32), SAT_BINS - 1)
        np.add.at(out, hi * SAT_BINS + si, s[croma])
    gris = ~croma
    if gris.any():
        out[-2] += float(np.count_nonzero(v[gris] >= 0.5)) * 0.25
        out[-1] += float(np.count_nonzero(v[gris] < 0.5)) * 0.25

    total = float(out.sum())
    return out / total if total else out


def embed(img: Image.Image) -> np.ndarray | None:
    """Vector L2-normalizado de la imagen, o None si no hay modelo."""
    sess = _load_session()
    if sess is None:
        return None
    im = _center_crop(img.convert('RGB')).resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)
    a = (np.asarray(im, dtype=np.float32) / 255.0 - _IMAGENET_MEAN) / _IMAGENET_STD
    salida = sess.run(None, {'pixel_values': a.transpose(2, 0, 1)[None]})[0][0]
    # CLS + media de parches: para buscar el mismo objeto concreto va mejor que
    # el CLS solo, que se queda en "que clase de cosa es".
    vec = np.concatenate([salida[0], salida[1:].mean(axis=0)])
    norma = float(np.linalg.norm(vec))
    return (vec / norma).astype(np.float32) if norma else None


def describe(img: Image.Image) -> tuple[np.ndarray | None, np.ndarray]:
    """(embedding, histograma). El histograma sale siempre; el embedding, si hay modelo."""
    return embed(img), color_histogram(img)


# ── Serializacion a BLOB (BYTEA en PostgreSQL, BLOB en SQLite) ───────────────

def to_bytes(vec: np.ndarray | None) -> bytes | None:
    return None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(raw: bytes | None, dim: int) -> np.ndarray | None:
    if not raw:
        return None
    vec = np.frombuffer(raw, dtype=np.float32)
    return vec if vec.size == dim else None


# ── Puntuacion ───────────────────────────────────────────────────────────────

def score(emb_a: np.ndarray | None, hist_a: np.ndarray | None,
          emb_b: np.ndarray | None, hist_b: np.ndarray | None) -> float | None:
    """Parecido entre dos fotos, de 0 a 1. None si faltan los descriptores."""
    if emb_a is None or emb_b is None:
        return None
    forma = float(np.dot(emb_a, emb_b))
    if hist_a is None or hist_b is None:
        return max(0.0, min(1.0, forma))
    color = float(np.minimum(hist_a, hist_b).sum())   # interseccion de histogramas
    return max(0.0, min(1.0, (1.0 - COLOR_WEIGHT) * forma + COLOR_WEIGHT * color))


def band(value: float) -> str:
    """Banda en vez de porcentaje: un 0.82 de coseno no es un 82% de probabilidad,
    y ensenarlo como tal invita a devolver la prenda a quien no toca."""
    if value >= BAND_HIGH:
        return 'alta'
    if value >= BAND_MEDIUM:
        return 'media'
    return 'baja'


def rank(emb_q: np.ndarray, hist_q: np.ndarray, candidates,
         floor: float = SCORE_FLOOR, limit: int = MAX_RESULTS) -> list[tuple]:
    """Ordena candidatos de mas a menos parecido.

    `candidates` es un iterable de (objeto, embedding, histograma). Devuelve
    [(objeto, puntuacion, banda)] recortado a `limit`, sin nada por debajo de
    `floor`.
    """
    puntuados = []
    for obj, emb, hist in candidates:
        valor = score(emb_q, hist_q, emb, hist)
        if valor is not None and valor >= floor:
            puntuados.append((obj, valor, band(valor)))
    puntuados.sort(key=lambda fila: -fila[1])
    return puntuados[:limit]

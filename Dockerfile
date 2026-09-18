FROM python:3.10-slim

WORKDIR /app

# Timezone
ENV TZ=Europe/Madrid
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Dependencias del sistema para Pillow (JPEG/PNG) y onnxruntime (libgomp1:
# la imagen slim no lo trae y onnxruntime lo enlaza en tiempo de ejecucion)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev curl libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Modelo de imagen para identificar objetos perdidos (DINOv2-small, cuantizado).
# No va al repositorio: son 23 MB de binario y el repositorio es publico.
#
# La descarga NO es fatal a proposito. Si Hugging Face no responde, el despliegue
# tiene que seguir: la aplicacion detecta que falta el fichero, esconde el boton
# de identificar y el resto funciona igual. Un CDN caido no puede bloquear un
# despliegue que venia a arreglar otra cosa.
# Comprobar despues con: docker exec <CONTENEDOR> ls -la /app/models
ARG IMAGE_MODEL_URL=https://huggingface.co/Xenova/dinov2-small/resolve/main/onnx/model_quantized.onnx
ARG IMAGE_MODEL_SHA=3afdc8bc63b50558d6e5770f5b799bb82455c2311183a2de43803f343a29d917
RUN mkdir -p /app/models && \
    (curl -fsSL "$IMAGE_MODEL_URL" -o /tmp/modelo.onnx && \
     echo "$IMAGE_MODEL_SHA  /tmp/modelo.onnx" | sha256sum -c - && \
     mv /tmp/modelo.onnx /app/models/dinov2-small-q.onnx && \
     echo "Modelo de imagen instalado.") || \
    echo "AVISO: no se pudo descargar el modelo de imagen. La identificacion de objetos quedara desactivada."

# Non-root user for security
RUN useradd -m -u 1000 app

COPY . .

# Crear directorios para datos persistentes (owned by app user)
RUN mkdir -p instance uploads/residents uploads/selfies uploads/signing_selfies && \
    chown -R app:app /app

USER app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "2", "--timeout", "120", "run:app"]

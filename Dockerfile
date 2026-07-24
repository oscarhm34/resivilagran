FROM python:3.10-slim

WORKDIR /app

# Dependencias del sistema para Pillow (JPEG/PNG)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Crear directorios para datos persistentes
RUN mkdir -p instance uploads/residents uploads/selfies uploads/signing_selfies

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "run:app"]

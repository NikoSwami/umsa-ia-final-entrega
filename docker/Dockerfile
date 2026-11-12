# Gaceta IA en MODO API: chat + busqueda + carga manual, sin GPU ni Qwen.
# Es lo mismo que colab/Ejecutar_App_Colab1.ipynb, en un contenedor.
#
#   docker compose up -d --build        (ver docker-compose.yml)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/gaceta/.cache/huggingface \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false \
    GACETA_BACKEND=api

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch desde el indice CPU (el de PyPI arrastra ~2 GB de CUDA que aqui no se
# usan). Va en su propia capa: es lo mas pesado y lo que menos cambia.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.10.0
COPY docker/requirements-api.txt /tmp/requirements-api.txt
RUN pip install -r /tmp/requirements-api.txt

# Usuario sin privilegios. Las carpetas que se montan como volumen se crean
# aqui con su dueño: un volumen nuevo hereda dueño y contenido de la imagen.
RUN useradd --create-home --uid 1000 gaceta \
 && mkdir -p /app/modelos_mejoras /home/gaceta/.cache/huggingface \
 && chown -R gaceta:gaceta /app /home/gaceta/.cache

# El codigo y la base (raw, chunks, FAISS, grafos). La base llena el volumen
# de datos la primera vez; despues manda el volumen (ver docker-compose.yml).
COPY --chown=gaceta:gaceta . /app

USER gaceta
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"

CMD ["python", "docker/arrancar.py"]

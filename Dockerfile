FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/OrganoidSchnitzel/immich-metadatenmaster" \
      org.opencontainers.image.description="Suggest dates and locations for photos in Immich: from faces, file names and GPX tracks" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf-cache

# CPU-only PyTorch by default (small, runs everywhere). For an AMD GPU with ROCm, build with e.g.
#   --build-arg TORCH_INDEX=https://download.pytorch.org/whl/rocm6.2
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
# torch and torchvision (needed by timm) must come from the same index; the constraints file keeps the
# following install from replacing them with mismatching builds from PyPI.
RUN pip install torch torchvision --index-url ${TORCH_INDEX} \
 && pip freeze | grep -iE "^(torch|torchvision)==" > /tmp/torch-constraints.txt

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[mivolo]" -c /tmp/torch-constraints.txt

# MiVOLO's model package is not on PyPI: fetch it from a pinned commit and adapt it to current timm.
RUN immich-metadatenmaster install-mivolo

VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["immich-metadatenmaster", "serve", "--port", "8080"]

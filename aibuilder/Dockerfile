FROM python:3.13-slim AS base

WORKDIR /aibuilder

# `git` is needed by tools.clone_repo (shells out to `git clone`).
# `ca-certificates` is needed for HTTPS git clones from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         git \
         ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# OpenTofu 1.8.x — pinned to match CI; deploys run from inside this image.
ARG TOFU_VERSION=1.8.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip wget \
    && wget -q "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" -O /tmp/tofu.zip \
    && unzip /tmp/tofu.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/tofu \
    && rm /tmp/tofu.zip \
    && apt-get purge -y unzip wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

# Use a non-default port to make ALB / port-collision debugging clearer.
EXPOSE 8001

# Cloud runs the immutable image with no --reload. Local dev gets
# hot-reload via the docker-compose volume mount + a different CMD
# override (see docker-compose.yml).
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]

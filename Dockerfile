FROM python:3.12-slim
WORKDIR /srv
# iproute2 builds the helper interface for reaching macvlan containers
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY heimdall/Stowaway ./heimdall/Stowaway
RUN python -m compileall -q app
# Shown in Settings → Diagnostics; set by the GitHub build from the release tag.
ARG VERSION=""
ENV STOWAWAY_VERSION=${VERSION}
# MALLOC_ARENA_MAX keeps glibc from reserving extra memory pools for short-lived threads.
ENV STOWAWAY_CONFIG=/config/config.yaml PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
CMD ["python", "-m", "app.main"]

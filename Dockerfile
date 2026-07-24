# MicroEMS – optionaler Container-Betrieb.
#
# Das lokale Setup (/opt/ems + systemd-Dienst) bleibt davon UNBERÜHRT; dieses
# Image ist eine eigenständige Alternative. Config und persistente Daten werden
# zur Laufzeit gemountet (nicht ins Image gebaut) – so bleiben Secrets außen vor.
FROM python:3.13-slim

# Zeitzone (Fahrpläne/Slots rechnen lokal) + CBC als Solver-Fallback.
# HiGHS (Standard-Solver, deterministisch) kommt via pip (highspy).
ENV TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata coinor-cbc ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements zuerst -> Docker-Layer-Cache für die (teure) Installation.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "highspy>=1.7"

# Nur das Anwendungspaket; Config/Daten kommen als Mounts.
COPY ems ./ems

# Dashboard-HTTP-Server (config: dashboard.port, Default 8080).
EXPOSE 8080

# Healthcheck über den Dashboard-/version-Endpunkt (nur sinnvoll bei
# dashboard.serve=true; sonst im compose entfernen).
HEALTHCHECK --interval=60s --timeout=8s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/version',timeout=6).status==200 else 1)" || exit 1

# Dauerbetrieb mit gemounteter Config. Argumente über compose/CLI überschreibbar
# (z.B. `docker run ... microems --config /app/config/config.yaml --check`).
ENTRYPOINT ["python", "-m", "ems.main"]
CMD ["--config", "/app/config/config.yaml", "--loop"]

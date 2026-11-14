# MicroEMS – optionaler Container-Betrieb.
#
# Das lokale Setup (/opt/ems + systemd-Dienst) bleibt davon UNBERÜHRT; dieses
# Image ist eine eigenständige Alternative. Config und persistente Daten werden
# zur Laufzeit gemountet (nicht ins Image gebaut) – so bleiben Secrets außen vor.
FROM python:3.14-slim

# Zeitzone (Fahrpläne/Slots rechnen lokal) + CBC als Solver-Fallback.
# HiGHS (Standard-Solver, deterministisch) kommt via pip (highspy).
ENV TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata coinor-cbc ca-certificates cron \
    && ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime \
    && echo "Europe/Berlin" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements zuerst -> Docker-Layer-Cache für die (teure) Installation.
COPY requirements.txt requirements.lock ./
# Laufzeitpakete aus requirements.txt, Versionen aus requirements.lock als
# Constraint (-c). Ohne den Lock zog jeder Build die neuesten Versionen - auch
# ein neues highspy, das laut requirements.txt erst nach einem Replay echter
# Folgelaeufe uebernommen werden darf. Bewusst -c statt -r requirements.lock:
# der Lock ist ein `pip freeze` der ganzen Entwicklungsumgebung (pytest, ruff),
# die nicht ins Image gehoert.
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

# Anwendungspaket + die für die geplanten Jobs nötigen Top-Level-Skripte.
# (Config/Daten kommen als Mounts.)
COPY ems ./ems
COPY kalibrierung.py savings_check.py backup.sh ./
COPY docker ./docker
RUN chmod +x /app/docker/scheduler-run.sh /app/backup.sh \
    && install -m 0644 /app/docker/crontab /etc/cron.d/ems

# Unprivilegierter Benutzer fuer den EMS-Dienst. Das Image selbst bleibt bei
# root als Standard, weil (a) der scheduler-Container `cron -f` startet, das
# root braucht, und (b) bestehende ./config- und ./data-Mounts root gehoeren -
# ein fester USER wuerde laufende Installationen beim naechsten Update am
# Schreiben hindern. Der Wechsel geschieht deshalb je Dienst im compose
# (`user: "10001:10001"`, siehe docker-compose.yml) nach einmaligem chown.
RUN groupadd --system --gid 10001 ems \
    && useradd --system --uid 10001 --gid ems --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin ems

# Dashboard-HTTP-Server (config: dashboard.port, Default 8080).
EXPOSE 8080

# Healthcheck über den Dashboard-/version-Endpunkt (nur sinnvoll bei
# dashboard.serve=true; sonst im compose entfernen). 401/403 zaehlen als
# "lebt": mit gesetztem dashboard.password antwortet der Server ohne
# Zugangsdaten mit 401 - der Container galt dann dauerhaft als unhealthy.
# Abweichender dashboard.port: EMS_HEALTH_PORT setzen.
HEALTHCHECK --interval=60s --timeout=8s --start-period=90s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

# Dauerbetrieb mit gemounteter Config. Argumente über compose/CLI überschreibbar
# (z.B. `docker run ... microems --config /app/config/config.yaml --check`).
ENTRYPOINT ["python", "-m", "ems.main"]
CMD ["--config", "/app/config/config.yaml", "--loop"]

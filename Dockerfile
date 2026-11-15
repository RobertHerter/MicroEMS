# MicroEMS – optionaler Container-Betrieb.
#
# Das lokale Setup (/opt/ems + systemd-Dienst) bleibt davon UNBERÜHRT; dieses
# Image ist eine eigenständige Alternative. Config und persistente Daten werden
# zur Laufzeit gemountet (nicht ins Image gebaut) – so bleiben Secrets außen vor.
FROM python:3.14-slim

# Zeitzone (Fahrpläne/Slots rechnen lokal) + CBC als Solver-Fallback.
# HiGHS (Standard-Solver, deterministisch) kommt via pip (highspy).
# setpriv (util-linux, im Basis-Image enthalten) gibt im Entrypoint die
# root-Rechte ab; der Aufruf unten laesst den Build scheitern, falls ein
# kuenftiges Basis-Image es nicht mehr mitbringt.
ENV TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata coinor-cbc ca-certificates cron \
    && setpriv --version \
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
# Einmal-Skripte, die docs/howto-docker.md im Container aufruft (Historie
# importieren, Wetter/Preise nachladen, Planwert auswerten). Sie fehlten im
# Image - die Anleitung lief ins Leere.
COPY rscp_import.py weather_backfill.py energycharts_backfill.py plan_value.py ./
COPY docker ./docker
RUN chmod +x /app/docker/scheduler-run.sh /app/docker/entrypoint.sh /app/backup.sh \
    && install -m 0644 /app/docker/crontab /etc/cron.d/ems \
    && ln -s /app/docker/entrypoint.sh /usr/local/bin/ems-run

# Unprivilegierter Benutzer fuer den EMS-Dienst und die geplanten Jobs.
# Bewusst KEIN `USER ems`: der Container startet als root, damit
# (a) docker/entrypoint.sh bestehende, root-eigene ./config- und ./data-Mounts
#     einmalig an ems uebergeben kann - ein fester USER wuerde laufende
#     Installationen beim naechsten Update am Schreiben hindern - und
# (b) der scheduler-Container `cron -f` starten kann, das root braucht.
# Die Rechte gibt der Entrypoint (bzw. scheduler-run.sh je Job) per setpriv ab.
#
# /app selbst (nur das Verzeichnis, nicht der Code darin) gehoert ems: die
# Vorgaben der Config schreiben relativ zum Arbeitsverzeichnis
# (./last_run_debug.json, ./savings_state.json ...). Wer diese Pfade nicht auf
# /app/data umgestellt hat, schrieb bisher als root nach /app - das muss
# weiter gehen.
# compileall: ems darf neben dem root-eigenen Code keine __pycache__ anlegen;
# ohne vorab uebersetzten Bytecode wuerde jeder Start alles neu kompilieren.
RUN groupadd --system --gid 10001 ems \
    && useradd --system --uid 10001 --gid ems --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin ems \
    && chown ems:ems /app \
    && python -m compileall -q /app/ems /app/kalibrierung.py /app/savings_check.py

# Dashboard-HTTP-Server (config: dashboard.port, Default 8080).
EXPOSE 8080

# Healthcheck über den Dashboard-/version-Endpunkt (nur sinnvoll bei
# dashboard.serve=true; sonst im compose entfernen). 401/403 zaehlen als
# "lebt": mit gesetztem dashboard.password antwortet der Server ohne
# Zugangsdaten mit 401 - der Container galt dann dauerhaft als unhealthy.
# Abweichender dashboard.port: EMS_HEALTH_PORT setzen.
HEALTHCHECK --interval=60s --timeout=8s --start-period=90s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

# Dauerbetrieb mit gemounteter Config. Der Entrypoint gibt die root-Rechte ab
# und startet `python -m ems.main` mit den Argumenten; sie sind über
# compose/CLI überschreibbar
# (z.B. `docker run ... microems --config /app/config/config.yaml --check`).
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["--config", "/app/config/config.yaml", "--loop"]

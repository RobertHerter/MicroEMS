"""Container-Healthcheck: lebt der Dashboard-Server?

Fragt /version ab. Jede HTTP-Antwort des Servers unter 500 zaehlt als "lebt" -
ausdruecklich auch 401/403: mit gesetztem dashboard.password antwortet der
Server ohne Zugangsdaten mit 401, und der Container galt frueher dauerhaft als
unhealthy. Keine Antwort (Verbindung abgelehnt, Zeitlimit) oder 5xx = krank.

Port: EMS_HEALTH_PORT, sonst 8080 (dashboard.port-Vorgabe).
"""
import os
import sys
import urllib.error
import urllib.request


def check(url: str, timeout: float = 6.0) -> bool:
    try:
        status = urllib.request.urlopen(url, timeout=timeout).status
    except urllib.error.HTTPError as exc:      # 4xx/5xx: der Server hat geantwortet
        status = exc.code
    except Exception:                          # keine Antwort
        return False
    return status < 500


if __name__ == "__main__":
    port = os.environ.get("EMS_HEALTH_PORT", "8080")
    sys.exit(0 if check(f"http://localhost:{port}/version") else 1)

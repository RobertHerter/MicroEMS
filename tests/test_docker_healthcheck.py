"""Container-Healthcheck: 401 heisst "lebt".

Mit gesetztem dashboard.password antwortet /version ohne Zugangsdaten mit 401.
Der alte Healthcheck (urlopen, status == 200) warf dabei eine Ausnahme - der
Container galt dauerhaft als unhealthy.
"""
from __future__ import annotations

import importlib.util
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_PFAD = pathlib.Path(__file__).resolve().parents[1] / "docker" / "healthcheck.py"
_spec = importlib.util.spec_from_file_location("ems_docker_healthcheck", _PFAD)
healthcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(healthcheck)


def _server(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("status,lebt", [
    (200, True), (401, True), (403, True), (500, False), (503, False)])
def test_healthcheck_counts_auth_errors_as_alive(status, lebt):
    server = _server(status)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/version"
        assert healthcheck.check(url, timeout=3) is lebt
    finally:
        server.shutdown()
        server.server_close()


def test_healthcheck_fails_without_a_server():
    server = _server(200)
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    assert healthcheck.check(f"http://127.0.0.1:{port}/version", timeout=1) is False


def test_dockerfile_installs_against_the_lock_and_uses_the_script():
    """Der Build darf nicht die jeweils neuesten Versionen ziehen - highspy
    wird laut requirements.txt nur nach einem Replay uebernommen."""
    wurzel = _PFAD.parents[1]
    docker = (wurzel / "Dockerfile").read_text(encoding="utf-8")
    assert "-r requirements.txt -c requirements.lock" in docker
    assert "docker/healthcheck.py" in docker
    ci = (wurzel / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "-c requirements.lock" in ci

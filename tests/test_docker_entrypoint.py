"""Container ohne root: statische Pruefung der Start-Skripte.

Das Laufzeitverhalten (Mounts uebergeben, Rechte abgeben) laesst sich nur im
Container pruefen. Hier wird festgehalten, was dabei nicht verloren gehen darf:
  * die Skripte sind gueltiges POSIX-sh (im Image laeuft dash),
  * das Image startet als root (kein USER) ueber den Entrypoint - nur so kann
    er root-eigene Mounts bestehender Installationen uebergeben,
  * die cron-Zeilen behalten root im Benutzerfeld: nur root darf die Ausgabe
    nach /proc/1/fd/1 umleiten; die Rechte gibt scheduler-run.sh selbst ab.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKRIPTE = ["docker/privs.sh", "docker/entrypoint.sh", "docker/scheduler-run.sh"]


@pytest.mark.parametrize("skript", _SKRIPTE)
def test_scripts_are_valid_posix_sh(skript):
    shell = shutil.which("dash") or shutil.which("sh")
    if not shell:
        pytest.skip("keine POSIX-Shell vorhanden")
    res = subprocess.run([shell, "-n", str(_ROOT / skript)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_image_starts_as_root_through_the_entrypoint():
    zeilen = [z.strip() for z in (_ROOT / "Dockerfile").read_text().splitlines()]
    assert not [z for z in zeilen if z.startswith("USER ")]
    assert 'ENTRYPOINT ["/app/docker/entrypoint.sh"]' in zeilen


def test_both_scripts_drop_privileges_before_the_work():
    for skript in ("docker/entrypoint.sh", "docker/scheduler-run.sh"):
        text = (_ROOT / skript).read_text()
        assert ". /app/docker/privs.sh" in text
        assert "ems_prepare" in text and "ems_exec_dropped" in text
    lauf = (_ROOT / "docker/scheduler-run.sh").read_text()
    assert lauf.index("ems_exec_dropped") < lauf.index("python ")


def test_cron_jobs_keep_root_in_the_user_field():
    jobs = [z.split() for z in (_ROOT / "docker/crontab").read_text().splitlines()
            if z.strip() and not z.startswith("#") and "=" not in z.split()[0]]
    assert jobs
    for felder in jobs:
        assert felder[5] == "root"
        assert felder[6] == "/app/docker/scheduler-run.sh"
        assert ">>/proc/1/fd/1" in felder

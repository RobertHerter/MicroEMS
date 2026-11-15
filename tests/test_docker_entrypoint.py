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


def test_crontab_has_the_weekly_backup_job():
    """Montag 03:30 wie ems-backup.timer, Benutzerfeld und Umleitung wie bei
    den anderen Jobs (die Rechte gibt scheduler-run.sh ab)."""
    jobs = [z.split() for z in (_ROOT / "docker/crontab").read_text().splitlines()
            if z.strip() and not z.startswith("#") and "=" not in z.split()[0]]
    backup = [f for f in jobs if f[7:8] == ["backup"]]
    assert len(backup) == 1
    felder = backup[0]
    assert felder[:5] == ["30", "3", "*", "*", "1"]
    assert felder[5] == "root"
    assert felder[6] == "/app/docker/scheduler-run.sh"
    assert felder[8:] == [">>/proc/1/fd/1", "2>&1"]
    timer = (_ROOT / "ems-backup.timer").read_text()
    assert "OnCalendar=Mon *-*-* 03:30:00" in timer


def test_backup_job_collects_both_mounts_and_runs_after_the_drop():
    lauf = (_ROOT / "docker/scheduler-run.sh").read_text()
    zweig = lauf[lauf.index("  backup)"):]
    zweig = zweig[:zweig.index(";;")]
    assert lauf.index("ems_exec_dropped") < lauf.index("  backup)")
    assert "umask 077" in zweig                    # Archiv enthaelt Zugangsdaten
    assert 'EMS_SRC="$(dirname "$CFG")"' in zweig  # config.yaml + Overlay
    assert "EMS_SRC_EXTRA=/app/data" in zweig      # Profile, savings_state
    assert "/app/backup.sh" in zweig and "/app/data/backup" in zweig
    # cron reicht die Umgebung nicht weiter: vor der Rechteabgabe holen.
    assert lauf.index("ems_pid1_env EMS_BACKUP_DIR") < lauf.index("ems_prepare\n")
    # Die Historie nur auf ausdruecklichen Wunsch - nie fest eingeschaltet.
    assert "EMS_BACKUP_DB=1" not in lauf.replace("EMS_BACKUP_DB=1 am", "")
    assert "COPY kalibrierung.py savings_check.py backup.sh ./" in (
        _ROOT / "Dockerfile").read_text()

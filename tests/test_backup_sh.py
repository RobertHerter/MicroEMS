"""backup.sh: die Historie muss sich sichern lassen, WAEHREND geschrieben wird.

Der optionale DB-Schritt (EMS_BACKUP_DB=1) nutzte ``sqlite3 .backup``. Die
Online-Sicherung beginnt bei jeder Aenderung der Quelle durch eine andere
Verbindung von vorn - der Dienst schreibt alle 5 s, am Live-System lief ein
Versuch ueber 10 min ohne Ende. ``VACUUM INTO`` liest dagegen in einer einzigen
Lesetransaktion. Der Test laesst deshalb einen zweiten Schreiber laufen: mit
``.backup`` endet er im Zeitlimit.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import threading

import pytest

_SKRIPT = pathlib.Path(__file__).resolve().parents[1] / "backup.sh"


def _sqlite3_cli_ok() -> bool:
    exe = shutil.which("sqlite3")
    if not exe or not shutil.which("bash"):
        return False
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    try:
        version = tuple(int(x) for x in out.split()[0].split(".")[:2])
    except (IndexError, ValueError):
        return False
    return version >= (3, 27)


pytestmark = pytest.mark.skipif(
    not _sqlite3_cli_ok(), reason="braucht bash und die sqlite3-CLI >= 3.27")


def _quelle(tmp_path: pathlib.Path) -> pathlib.Path:
    src = tmp_path / "ems"
    src.mkdir()
    (src / "config.yaml").write_text("general: {}\n")
    con = sqlite3.connect(src / "e3dc_history.sqlite")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE live_samples (ts INTEGER PRIMARY KEY, fuell BLOB)")
    # Einige tausend Seiten: .backup kopiert in Schritten zu 100 Seiten und
    # wird so vom Schreiber sicher unterbrochen.
    con.executemany("INSERT INTO live_samples VALUES (?, ?)",
                    ((i, os.urandom(2000)) for i in range(8000)))
    con.commit()
    con.close()
    return src


def _lauf(src: pathlib.Path, dest: pathlib.Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SKRIPT), str(dest)], capture_output=True, text=True,
        timeout=60, env={**os.environ, "EMS_SRC": str(src), **env})


def test_db_backup_finishes_while_another_connection_writes(tmp_path):
    src = _quelle(tmp_path)
    dest = tmp_path / "ziel"
    stop = threading.Event()
    geschrieben = []

    def schreiber():
        con = sqlite3.connect(src / "e3dc_history.sqlite", timeout=30)
        i = 1_000_000
        while not stop.is_set():
            con.execute("INSERT INTO live_samples VALUES (?, ?)", (i, b"x" * 500))
            con.commit()
            i += 1
            geschrieben.append(i)
            stop.wait(0.002)
        con.close()

    t = threading.Thread(target=schreiber, daemon=True)
    t.start()
    try:
        res = _lauf(src, dest, EMS_BACKUP_DB="1")
    finally:
        stop.set()
        t.join(timeout=30)

    assert res.returncode == 0, res.stderr
    assert geschrieben, "der Schreiber lief nicht - der Test prueft sonst nichts"
    sicherungen = list(dest.glob("ems-history-*.sqlite"))
    assert len(sicherungen) == 1
    assert not list(dest.glob("*.teil"))
    assert (sicherungen[0].stat().st_mode & 0o777) == 0o600
    con = sqlite3.connect(sicherungen[0])
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Konsistenter Stand: der Grundbestand vollstaendig, dazu ein Praefix
    # dessen, was der Schreiber bis zum Beginn der Lesetransaktion ablegte.
    assert con.execute(
        "SELECT COUNT(*) FROM live_samples WHERE ts < 1000000").fetchone()[0] == 8000
    con.close()


def test_db_backup_is_off_by_default(tmp_path):
    src = _quelle(tmp_path)
    dest = tmp_path / "ziel"
    res = _lauf(src, dest)
    assert res.returncode == 0, res.stderr
    assert len(list(dest.glob("ems-config-*.tar.gz"))) == 1
    assert not list(dest.glob("ems-history-*"))


def test_db_backup_handles_quote_in_target_path(tmp_path):
    src = _quelle(tmp_path)
    dest = tmp_path / "ziel mit ' hochkomma"
    res = _lauf(src, dest, EMS_BACKUP_DB="1")
    assert res.returncode == 0, res.stderr
    assert len(list(dest.glob("ems-history-*.sqlite"))) == 1


def _inhalt(archiv: pathlib.Path) -> dict:
    import tarfile
    with tarfile.open(archiv) as tar:
        return {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers()}


def test_backup_collects_files_from_two_directories(tmp_path):
    """Docker: Config und Overlay liegen unter /app/config, Profile und
    Ersparnis-Status unter /app/data. Je Dateiname zaehlt der erste Fundort,
    das Archiv bleibt flach und nur fuer den Eigentuemer lesbar."""
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir()
    (config / "config.yaml").write_text("geheim: config\n")
    (config / "config_overrides.yaml").write_text("overlay: 1\n")
    (data / "kalibrierung.yaml").write_text("k: 1\n")
    (data / "kalibrierung_profil.yaml").write_text("profil: 1\n")
    (data / "savings_state.json").write_text("{}\n")
    (data / "config.yaml").write_text("geheim: falscher-ort\n")   # verliert
    dest = data / "backup"

    res = _lauf(config, dest, EMS_SRC_EXTRA=str(data))

    assert res.returncode == 0, res.stderr
    archive = list(dest.glob("ems-config-*.tar.gz"))
    assert len(archive) == 1
    assert (archive[0].stat().st_mode & 0o777) == 0o600
    inhalt = _inhalt(archive[0])
    assert sorted(inhalt) == ["config.yaml", "config_overrides.yaml",
                              "kalibrierung.yaml", "kalibrierung_profil.yaml",
                              "savings_state.json"]
    assert inhalt["config.yaml"] == "geheim: config\n"
    assert not list(dest.glob("ems-history-*"))


def test_backup_with_relative_source_directories(tmp_path):
    """tar wertet ein relatives -C relativ zum vorigen -C aus - die Quellen
    muessen deshalb absolut uebergeben werden."""
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("a: 1\n")
    (tmp_path / "data" / "savings_state.json").write_text("{}\n")
    res = subprocess.run(
        ["bash", str(_SKRIPT), "ziel"], capture_output=True, text=True,
        timeout=60, cwd=tmp_path,
        env={**os.environ, "EMS_SRC": "config", "EMS_SRC_EXTRA": "data"})
    assert res.returncode == 0, res.stderr
    archiv = next((tmp_path / "ziel").glob("ems-config-*.tar.gz"))
    assert sorted(_inhalt(archiv)) == ["config.yaml", "savings_state.json"]


def test_single_source_directory_still_works_and_keeps_eight(tmp_path):
    src = tmp_path / "ems"
    src.mkdir()
    (src / "config.yaml").write_text("a: 1\n")
    dest = tmp_path / "ziel"
    dest.mkdir()
    for i in range(10):
        alt = dest / f"ems-config-2026010{i}-000000.tar.gz"
        alt.write_text("alt")
        os.utime(alt, (1_700_000_000 + i, 1_700_000_000 + i))
    res = _lauf(src, dest)
    assert res.returncode == 0, res.stderr
    assert len(list(dest.glob("ems-config-*.tar.gz"))) == 8
    assert not (dest / "ems-config-20260100-000000.tar.gz").exists()


def test_db_backup_falls_back_to_python_without_sqlite3_cli(tmp_path):
    """Das Docker-Image bringt die sqlite3-CLI nicht mit, python3 aber immer."""
    src = _quelle(tmp_path)
    dest = tmp_path / "ziel"
    werkzeuge = tmp_path / "bin"
    werkzeuge.mkdir()
    for name in ("bash", "python3", "tar", "gzip", "date", "ls", "tail",
                 "xargs", "rm", "chmod", "mv", "mkdir", "sort", "head", "awk"):
        pfad = shutil.which(name)
        assert pfad, name
        (werkzeuge / name).symlink_to(pfad)
    res = subprocess.run(
        [str(werkzeuge / "bash"), str(_SKRIPT), str(dest)],
        capture_output=True, text=True, timeout=120,
        env={"PATH": str(werkzeuge), "EMS_SRC": str(src), "EMS_BACKUP_DB": "1"})
    assert res.returncode == 0, res.stderr
    sicherungen = list(dest.glob("ems-history-*.sqlite"))
    assert len(sicherungen) == 1
    assert not list(dest.glob("*.teil"))
    assert (sicherungen[0].stat().st_mode & 0o777) == 0o600
    con = sqlite3.connect(sicherungen[0])
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0] == 8000
    con.close()

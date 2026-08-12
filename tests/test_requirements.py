"""Der Lock muss halten, was die Anleitung verspricht.

`docs/howto-standalone.md` nennt `pip install -r requirements.lock` als den
reproduzierbaren Weg. Der frueher von Hand gepflegte Auszug war unvollstaendig:
es fehlten highspy (Solver), scikit-learn (ML-Lastmodell), pvlib (PV-Prognose),
pye3dc (RSCP-Anbindung) sowie ruff und pytest-xdist. Wer der Anleitung folgte,
bekam ein EMS ohne Solver, ohne PV-Modell und ohne Geraeteanbindung - und nichts
hat es gemerkt, weil niemand die Dateien gegeneinander gehalten hat.
"""
from __future__ import annotations

import pathlib

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

WURZEL = pathlib.Path(__file__).resolve().parent.parent
DEKLARIERT = ("requirements.txt", "requirements-dev.txt")
LOCK = WURZEL / "requirements.lock"


def _lock() -> dict:
    """{normalisierter Name: Version} aus dem Lock."""
    out = {}
    for zeile in LOCK.read_text().splitlines():
        z = zeile.split("#")[0].strip()
        if "==" in z:
            name, version = z.split("==", 1)
            out[name.lower().replace("_", "-")] = version.strip()
    return out


def _deklariert() -> list:
    aus = []
    for datei in DEKLARIERT:
        for zeile in (WURZEL / datei).read_text().splitlines():
            z = zeile.split("#")[0].strip()
            if z and not z.startswith("-"):
                aus.append((datei, Requirement(z)))
    return aus


def test_every_declared_package_is_locked():
    """Ohne diese Pruefung faellt ein fehlendes Paket erst dem naechsten
    Anwender auf, der der Anleitung folgt."""
    fehlt = [f"{r.name} ({datei})" for datei, r in _deklariert()
             if r.name.lower().replace("_", "-") not in _lock()]
    assert not fehlt, f"Im Lock fehlen: {', '.join(sorted(fehlt))}"


def test_locked_versions_satisfy_the_declarations():
    """Der Lock darf der Deklaration nicht widersprechen - sonst installiert der
    reproduzierbare Weg etwas anderes als der normale."""
    lock = _lock()
    verstoss = []
    for _datei, r in _deklariert():
        version = lock.get(r.name.lower().replace("_", "-"))
        if version and not r.specifier.contains(Version(version),
                                                prereleases=True):
            verstoss.append(f"{r.name}: Lock {version} erfuellt {r.specifier} nicht")
    assert not verstoss, "; ".join(verstoss)


def test_lock_pins_exact_versions():
    """Ein Lock mit >= waere keiner."""
    lose = [z.strip() for z in LOCK.read_text().splitlines()
            if z.split("#")[0].strip() and "==" not in z.split("#")[0]]
    assert not lose, f"Nicht exakt festgelegt: {lose}"


@pytest.mark.parametrize("paket", ["highspy", "scikit-learn", "pvlib",
                                   "pye3dc"])
def test_the_packages_that_were_missing_stay_locked(paket):
    """Namentlich, weil genau diese vier fehlten - und ohne sie laeuft das EMS
    ohne Solver, ohne Lastmodell, ohne PV-Prognose bzw. ohne Geraet."""
    assert paket in _lock()

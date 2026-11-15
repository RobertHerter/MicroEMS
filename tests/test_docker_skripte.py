"""Jedes Skript, das die Docker-Anleitung im Container aufruft, muss im
Image liegen (15.11.2026: vier fehlten)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_documented_scripts_are_copied_into_the_image():
    doc = (ROOT / "docs" / "howto-docker.md").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = set()
    for line in dockerfile.splitlines():
        if line.startswith("COPY "):
            copied.update(part for part in line.split()[1:-1])
    genannt = {name for name in re.findall(r"\b([a-z_]+\.py)\b", doc)
               if (ROOT / name).is_file()}
    assert genannt, "Anleitung nennt keine Skripte mehr - Test anpassen"
    fehlend = sorted(genannt - copied)
    assert not fehlend, f"im Image fehlen: {fehlend}"

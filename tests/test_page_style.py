"""Die drei ausgelieferten Seiten tragen dieselben Gestaltungsgrößen.

Dashboard, Lauf-Archiv und Konfigurationseditor bringen je einen eigenen
Stilblock mit. Sie waren auseinandergelaufen - leicht andere Linienfarbe,
anderes gedämpftes Grau, vier verschiedene Eckenradien, zwei Schattenrezepte -,
und das fiel niemandem auf, weil man immer nur eine Seite gleichzeitig sieht.

Geprüft wird über die QUELLE, nicht über gerenderte Seiten: welche Seite
überhaupt erscheint, hängt an der Konfiguration (der Editor ist abschaltbar).
"""
from __future__ import annotations

import pathlib
import re

import pytest

SEITEN = ["ems/dashboard.py", "ems/archive.py", "ems/config_editor.py"]
# Nur die Größen, die seitenübergreifend gleich aussehen MÜSSEN. Farben wie
# --blue oder --danger darf jede Seite für sich führen.
TOKENS = ["--r-card", "--r-ctl", "--line", "--muted", "--card"]


def _tokens(quelle: str, dark: bool) -> dict[str, str]:
    """Werte aus dem hellen bzw. dunklen Deklarationsblock einer Seite."""
    marke = "html.dark" if dark else ":root"
    start = quelle.index(marke, quelle.index("<style>"))
    block = quelle[start:start + 700]
    gefunden = {}
    for token in TOKENS:
        m = re.search(re.escape(token) + r":\s*([^;}]+)", block)
        if m:
            gefunden[token] = m.group(1).strip()
    return gefunden


@pytest.mark.parametrize("dark", [False, True], ids=["hell", "dunkel"])
def test_all_pages_share_the_same_design_tokens(dark):
    werte = {p: _tokens(pathlib.Path(p).read_text(encoding="utf-8"), dark)
             for p in SEITEN}
    referenz = werte["ems/dashboard.py"]
    if dark:
        # Der Dunkelblock erklärt nur die FARBEN neu; Radien erbt er aus
        # :root. Ein Test, der auch dort alle Größen verlangt, würde eine
        # überflüssige Wiederholung erzwingen.
        erwartet = sorted(set(referenz) & set(TOKENS))
        assert erwartet, "Dashboard überschreibt im Dunkelmodus nichts"
    else:
        erwartet = TOKENS
        assert set(referenz) == set(TOKENS), \
            f"Dashboard deklariert nicht alle Größen: {sorted(referenz)}"
    for seite, gefunden in werte.items():
        for token in erwartet:
            assert token in gefunden, f"{seite}: {token} fehlt"
            assert gefunden[token] == referenz[token], (
                f"{seite}: {token} = {gefunden[token]!r}, "
                f"Dashboard: {referenz[token]!r}")


def test_no_page_hardcodes_a_card_radius_beside_the_token():
    """Ein fester Radius neben dem Token laeuft beim naechsten Mal wieder weg.

    Erlaubt bleiben kleine Radien (Punkte, Pillen, Fortschrittsbalken) - die
    sind Formgebung, keine Kartenkante.
    """
    verdaechtig = {}
    for seite in SEITEN:
        text = pathlib.Path(seite).read_text(encoding="utf-8")
        treffer = [m.group(0) for m in
                   re.finditer(r"border-radius:\s*1[0-4]px", text)]
        if treffer:
            verdaechtig[seite] = treffer
    assert not verdaechtig, f"feste Kartenradien statt Token: {verdaechtig}"

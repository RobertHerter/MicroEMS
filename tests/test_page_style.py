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
TOKENS = ["--r-card", "--r-ctl", "--line", "--muted", "--card",
          "--ok", "--warn", "--bad", "--focus"]


def _tokens(quelle: str, dark: bool) -> dict[str, str]:
    """Werte aus dem hellen bzw. dunklen Deklarationsblock einer Seite."""
    marke = "html.dark" if dark else ":root"
    start = quelle.index(marke, quelle.index("<style>"))
    # Bis zur schliessenden Klammer lesen, NICHT ueber ein Fenster fester
    # Laenge: der Block waechst mit jedem neuen Token, und ein zu kurzes
    # Fenster liess die spaeteren Werte lautlos verschwinden. Ein zu langes
    # waere schlimmer - es haette in den Dunkelblock hineingelesen und dessen
    # Farben als die hellen gemeldet.
    block = quelle[start:quelle.index("}", start)]
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


@pytest.mark.parametrize("seite", SEITEN)
def test_every_page_shows_where_the_keyboard_is(seite):
    """Ohne sichtbaren Fokus weiss man beim Durchtabben nicht, wo man steht.

    Die Knoepfe tragen eigene Flaechen und Rahmen; darauf verschwindet der
    Standardring des Browsers fast. Keine der drei Seiten hatte eine
    Fokusregel - null Treffer fuer :focus in allen dreien.
    """
    quelle = pathlib.Path(seite).read_text(encoding="utf-8")
    assert ":focus-visible" in quelle, f"{seite}: keine Fokusregel"
    assert "outline: 2px solid var(--focus)" in quelle \
        or "outline:2px solid var(--focus)" in quelle, \
        f"{seite}: Fokusring nicht ueber das Token"


@pytest.mark.parametrize("seite", SEITEN)
def test_every_page_respects_reduced_motion(seite):
    """Drei Endlos-Animationen laufen dauerhaft (Statuspunkt, Zeitplan,
    Prognose). Ihre Bewegung traegt keine eigene Information - Zustand steht
    in Farbe und Text -, also darf sie abschaltbar sein."""
    quelle = pathlib.Path(seite).read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in quelle, f"{seite}: keine Regel dafuer"


def test_status_colours_are_not_hardcoded_in_tiles():
    """Die Statusfarben standen als feste Hexwerte in den Kacheln und
    wechselten mit hell/dunkel nicht mit. Das Gelb der Warnung kam damit auf
    Weiss auf 2,1:1 - ausgerechnet der Warnzustand war der am schlechtesten
    lesbare."""
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    zeilen = [z for z in quelle.splitlines()
              if ("farbe =" in z or "color = " in z)
              and any(h in z for h in ("#2ca02c", "#e6a700", "#d62728", '"#777"'))]
    assert not zeilen, f"feste Statusfarben statt Token: {zeilen}"


def _kontrast(a: str, b: str) -> float:
    """WCAG-Kontrastverhältnis zweier Hexfarben."""
    def leuchtdichte(hexwert: str) -> float:
        hexwert = hexwert.lstrip("#")
        if len(hexwert) == 3:                       # Kurzform wie #fff
            hexwert = "".join(z * 2 for z in hexwert)
        kanaele = [int(hexwert[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        kanaele = [k / 12.92 if k <= 0.03928 else ((k + 0.055) / 1.055) ** 2.4
                   for k in kanaele]
        return 0.2126 * kanaele[0] + 0.7152 * kanaele[1] + 0.0722 * kanaele[2]
    hell, dunkel = leuchtdichte(a), leuchtdichte(b)
    hoch, tief = max(hell, dunkel), min(hell, dunkel)
    return (hoch + 0.05) / (tief + 0.05)


@pytest.mark.parametrize("dark", [False, True], ids=["hell", "dunkel"])
def test_status_colours_stay_readable_on_their_own_background(dark):
    """Der eigentliche Anspruch, nicht nur "es gibt ein Token".

    Ein Token laesst sich jederzeit auf einen unlesbaren Wert setzen. Geprueft
    wird deshalb die Zahl: 4,5:1 gegen die Flaeche, auf der die Farbe steht.
    Vorher scheiterte genau hier das Gelb der Warnung mit 2,1:1 auf Weiss.
    """
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    werte = _tokens(quelle, dark)
    grund = werte["--card"] if werte["--card"].startswith("#") else "#ffffff"
    for token in ("--ok", "--warn", "--bad", "--muted"):
        verhaeltnis = _kontrast(werte[token], grund)
        assert verhaeltnis >= 4.5, (
            f"{token} = {werte[token]} auf {grund}: "
            f"{verhaeltnis:.2f}:1, nötig sind 4,5:1")


# Grosse Einzelwerte sind Funktionsabstaende (Platz unter festen Fussleisten),
# keine Rhythmusfrage - sie duerfen freihaendig bleiben.
FUNKTIONSABSTAND_AB = 24


@pytest.mark.parametrize("seite", SEITEN)
def test_spacing_and_type_follow_a_scale(seite):
    """Vorher war JEDE Ganzzahl von 1 bis 16 px als Abstand im Einsatz.

    17 verschiedene Werte auf 312 Verwendungen, dazu zehn Schriftgrade - das
    ist keine Skala, sondern Freihand, und genau daher kommt der unruhige
    Eindruck. Ein neuer freihändiger Wert faellt hier auf, bevor er sich
    einschleicht.
    """
    quelle = pathlib.Path(seite).read_text(encoding="utf-8")
    frei = []
    for m in re.finditer(r"\b(padding|margin|gap|row-gap|column-gap)"
                         r"(-top|-bottom|-left|-right)?: *([^;}\n]+)", quelle):
        for wert in re.findall(r"(\d+)px", m.group(3)):
            if int(wert) < FUNKTIONSABSTAND_AB:
                frei.append(f"{m.group(1)}: …{wert}px")
    for m in re.finditer(r"font-size: ?(\d+)px", quelle):
        frei.append(f"font-size: {m.group(1)}px")
    assert not frei, f"{seite}: freihändige Werte statt Skala: {sorted(set(frei))}"


@pytest.mark.parametrize("dark", [False, True], ids=["hell", "dunkel"])
def test_the_scale_itself_is_a_scale(dark):
    """Die Token muessen aufsteigend und ohne Dubletten sein - sonst waere die
    Skala nur eine andere Schreibweise fuer Freihand."""
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    if dark:
        pytest.skip("Groessen erben aus :root, der Dunkelblock aendert nur Farben")
    for praefix, anzahl in (("--s", 6), ("--t", 5)):
        werte = [int(re.search(r"(\d+)px", m).group(1)) for m in
                 re.findall(rf"{praefix}\d: *\d+px", quelle)][:anzahl]
        assert len(werte) == anzahl, f"{praefix}: {len(werte)} statt {anzahl} Stufen"
        assert werte == sorted(set(werte)), f"{praefix} nicht aufsteigend: {werte}"


@pytest.mark.parametrize("seite", ["ems/dashboard.py", "ems/archive.py"])
def test_values_keep_a_fixed_digit_width_without_a_typewriter_face(seite):
    """Feste Ziffernbreite ja, eigene Schriftfamilie nein.

    Die Werte schreiben sich alle fuenf Sekunden neu - ohne tabular-nums
    springt die Breite bei jeder Aktualisierung sichtbar. Eine
    Schreibmaschinenschrift dazu war zu viel: auf einer Seite mit vielen
    Kacheln kippt das Bild Richtung Telemetrie-Auswurf. Der Test haelt beide
    Haelften fest, damit weder die eine noch die andere zurueckkommt.
    """
    quelle = pathlib.Path(seite).read_text(encoding="utf-8")
    assert "tabular-nums" in quelle, "feste Ziffernbreite fehlt"
    assert "--font-num" in quelle, "kein Token fuer die Wertschrift"
    assert "monospace" not in quelle, \
        f"{seite}: Schreibmaschinenschrift ist zurueck"


def test_no_theme_rule_overrides_the_tile_family_colour():
    """Eine spaetere Themenregel darf die Familienfarbe nicht ausknipsen.

    Genau das passierte: eine alte ``html.dark``-Sammelregel setzte fuer
    ``.tile`` Hintergrund UND border-color auf feste Werte - und stand SPAETER
    im Stil als die Kachelregel selbst. Im hellen Thema war die Zugehoerigkeit
    damit sichtbar, im dunklen gar nicht. Der Fehler ist unsichtbar, solange
    man nur ein Thema anschaut.
    """
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    stil = quelle[quelle.index("<style>"):quelle.index("</style>")]
    # Kommentare zuerst entfernen: sie stehen direkt vor der Regel und kleben
    # sonst am ersten Selektor, der dadurch nie exakt "html.dark .tile" heisst.
    stil = re.sub(r"/\*.*?\*/", " ", stil, flags=re.S)
    # Regelweise zerlegen statt zeilenweise: Selektorlisten laufen ueber
    # mehrere Zeilen, ein einzeiliges Muster findet sie nicht - und ein Test,
    # der nichts findet, geht immer durch.
    schuldige = []
    for block in stil.split("}}"):
        if "{{" not in block:
            continue
        selektoren, regeln = block.rsplit("{{", 1)
        selektoren = selektoren[selektoren.rfind("}") + 1:]
        trifft_kachel = any(
            teil.strip() in ("html.dark .tile", ".tile")
            for teil in selektoren.split(","))
        if trifft_kachel and ("background" in regeln or "border-color" in regeln):
            if "html.dark" in selektoren:
                schuldige.append(" ".join(selektoren.split())[:90])
    assert not schuldige, \
        f"Themenregel ueberschreibt Kachelfarbe: {schuldige}"


def test_all_cards_share_one_recipe():
    """Vierzehn Kartenfamilien hatten je ein eigenes Rezept.

    Neun von Hand gemischte Rahmenfarben, ebenso viele Hintergruende und Radien
    von 7, 8 und 9 px nebeneinander. An einer einzelnen Karte sieht man das
    nie - erst im Nebeneinander, und dann als Unruhe, der man nicht ansieht,
    woher sie kommt. Ein neues eigenes Rezept faellt hier auf.
    """
    KARTEN = {".quality-item", ".calibration-card", ".decision-item",
              ".load-card", ".pvconf-card", ".mode-compare-card",
              ".plan-compare", ".ctl-section", ".detail-grid > div",
              ".calibration-change-list article", ".curve-box",
              ".battery-planner", ".schedule-item", ".forecast-day-loading"}
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    stil = re.sub(r"/\*.*?\*/", " ",
                  quelle[quelle.index("<style>"):quelle.index("</style>")],
                  flags=re.S)
    eigene, gemeinsam = [], False
    for block in stil.split("}}"):
        if "{{" not in block:
            continue
        selektoren, regeln = block.rsplit("{{", 1)
        selektoren = selektoren[selektoren.rfind("}") + 1:]
        teile = {" ".join(t.split()) for t in selektoren.split(",")}
        if KARTEN <= teile:
            gemeinsam = True
            continue
        for name in teile & KARTEN:
            if re.search(r"\b(background|border|border-radius)\s*:", regeln):
                eigene.append(f"{name}: {' '.join(regeln.split())[:44]}")
    assert gemeinsam, "keine gemeinsame Kartenregel gefunden"
    assert not eigene, f"Karten mit eigenem Rezept: {eigene}"


def test_no_page_hardcodes_a_divider_or_surface_colour():
    """Fest verdrahtete Grautoene werden im Dunkelmodus zu weissen Linien.

    Die Planentscheidungen waren eine Parallelkonstruktion zu .info-panel mit
    eigenen Farben: ihre Trennlinie stand auf einem hellen Grau und leuchtete
    im Dunkeln als weisse Linie, ihre Kopfzeile trug einen anderen Grund als
    alle uebrigen Panels. Solche Werte fallen nur im jeweils ANDEREN Thema auf.
    """
    quelle = pathlib.Path("ems/dashboard.py").read_text(encoding="utf-8")
    stil = re.sub(r"/\*.*?\*/", " ",
                  quelle[quelle.index("<style>"):quelle.index("</style>")],
                  flags=re.S)
    verdaechtig = []
    for block in stil.split("}}"):
        if "{{" not in block:
            continue
        selektoren, regeln = block.rsplit("{{", 1)
        selektoren = " ".join(selektoren[selektoren.rfind("}") + 1:].split())
        if "html.dark" in selektoren:
            continue
        for m in re.finditer(r"(border-bottom|border-top)"
                             r"\s*:\s*[^;]*?#([0-9a-fA-F]{6})", regeln):
            kanaele = [int(m.group(2)[i:i + 2], 16) for i in (0, 2, 4)]
            # Nur NEUTRALE Linien: eine Akzentfarbe auf einem Knopf oder Banner
            # ist gewollt, ein Grauton als Trennlinie gehoert an --line.
            if max(kanaele) - min(kanaele) < 24:
                verdaechtig.append(f"{selektoren[:40]} -> {m.group(0)[:44]}")
    assert not verdaechtig, \
        f"feste Farbe fuer Trennlinien statt --line: {verdaechtig}"

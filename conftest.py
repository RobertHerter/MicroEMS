"""Testweite Grundeinstellungen.

WICHTIG: Die Thread-Variablen stehen VOR jedem Import - OpenMP und die
BLAS-Bibliotheken lesen sie beim Laden, danach wirken sie nicht mehr.

Warum: pytest laeuft parallel (``-n auto``, siehe pytest.ini) und startet auf
diesem Rechner vier Worker. Jeder Worker liess numpy/sklearn zusaetzlich eigene
OpenMP-Threads starten - also bis zu 16 rechenwillige Threads auf vier Kernen.
Gemessen am 03.09.2026 an der vollen Suite (745 Tests):

    Standard (Threads frei)        731 s  (12:11)
    OMP/BLAS auf 1 Thread          174 s  ( 2:54)

Faktor 4,2, allein durch das Abschalten der Ueberbuchung. Sichtbar wurde es
daran, dass ein Test im PARALLELEN Lauf 73 s brauchte und seriell 2,0 s - die
Dauer war Wartezeit, nicht Rechenzeit.

Die Parallelisierung bleibt bei pytest (ein Worker je Kern); nur die
Bibliotheken innerhalb eines Workers rechnen einkernig. Fuer den PRODUKTIVEN
Lauf gilt das nicht - dort steht ein Prozess allein auf der Maschine.
"""
import os

for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_var, "1")

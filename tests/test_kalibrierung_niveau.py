"""PV-Korrektur: Champion und Challenger niveaubereinigt vergleichen.

Im 14-Tage-Pruefrahmen entscheidet in einer Truebphase sonst das Niveau - es
gewinnt das Profil, das zufaellig tiefer liegt, nicht das mit der richtigen
Zeitlage. Geprueft wird auf synthetischen Reihen:
  (a) richtige Form schlaegt falsche Form mit zufaellig passendem Niveau,
  (b) ein grob falsches Niveau des Kandidaten rutscht trotzdem nicht durch,
  (c) Tage mit fast keiner Energie destabilisieren den Vergleich nicht,
  (d) die Lastkorrektur wird weiter unskaliert verglichen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ems.calibration_competition import (
    LEVEL_FACTOR_BOUNDS, compare_point_forecasts, level_adjust)

TZ = "Europe/Berlin"
PV_OPTIONEN = {"level_adjusted": True, "level_method": "per_day",
               "max_raw_wape_increase_pct": 5.0}


def _klar(index, peak, versatz_h=0.0):
    """Klarer-Himmel-Tagesgang 08-16 Uhr, optional zeitlich verschoben."""
    lokal = index.tz_convert(TZ)
    stunde = lokal.hour + lokal.minute / 60.0 - versatz_h
    return pd.Series(
        peak * np.clip(np.sin(np.pi * (stunde - 8.0) / 8.0), 0.0, None),
        index=index)


def _truebphase(peak=500.0, tage=14):
    """Ist-Reihe einer Truebphase: 55-75 % des klaren Tagesgangs, je Tag
    anders, ohne Tagesgang im Verhaeltnis. Nur Tagslots (wie im Betrieb)."""
    index = pd.date_range("2026-11-01", periods=tage * 96, freq="15min", tz=TZ)
    klar = _klar(index, peak)
    index = index[klar.to_numpy() > 1.0]
    tag = pd.Index(index.tz_convert(TZ).date)
    nummer = pd.factorize(tag)[0]
    daempfung = 0.55 + 0.05 * (nummer % 5)
    return index, _klar(index, peak) * daempfung


def _fall_a(peak=500.0):
    """Champion: 45 Minuten zu spaet, Niveau trifft die Truebphase zufaellig.
    Kandidat: richtige Zeitlage, aber das (hoehere) Niveau normaler Wochen."""
    index, ist = _truebphase(peak)
    spaet = _klar(index, peak, versatz_h=0.75)
    champion = spaet * float(ist.sum() / spaet.sum())
    kandidat = _klar(index, peak) * 0.85
    return ist, champion, kandidat


def test_richtige_form_schlaegt_zufaellig_passendes_niveau():
    """(a) Unskaliert gewinnt der Champion (er liegt tiefer), niveaubereinigt
    der Kandidat (er hat die richtige Zeitlage) - und wird befoerdert.

    Die Anlage ist bewusst klein: die Bias-Schranke gilt in Watt und bleibt
    auf den unskalierten Reihen aktiv (siehe den Test darunter)."""
    ist, champion, kandidat = _fall_a()
    alt = compare_point_forecasts(ist, champion, kandidat, TZ)
    assert alt["promote"] is False
    assert alt["improvement_percent"] < 0
    assert "level_adjusted" not in alt

    neu = compare_point_forecasts(ist, champion, kandidat, TZ, **PV_OPTIONEN)
    assert neu["promote"] is True and neu["status"] == "promoted"
    assert neu["comparison"] == "level_adjusted"
    # beide Zahlenpaare sind ausgewiesen
    assert neu["raw_improvement_percent"] == alt["improvement_percent"]
    assert neu["champion"] == alt["champion"]
    assert neu["challenger"] == alt["challenger"]
    bereinigt = neu["level_adjusted"]
    assert bereinigt["challenger"]["wape_pct"] < 1.0      # reine Form: trifft
    assert bereinigt["champion"]["wape_pct"] > 10.0
    assert neu["improvement_percent"] == bereinigt["improvement_percent"] > 50
    assert "niveaubereinigt" in neu["reason"] and "unskaliert" in neu["reason"]
    assert bereinigt["method"] == "per_day"
    assert bereinigt["days_own_factor"] == bereinigt["days"] == 14


def test_bias_schranke_bleibt_unskaliert_wirksam():
    """Derselbe Fall auf einer vierfach groesseren Anlage: die Form ist
    weiterhin besser, aber der unskalierte Bias steigt ueber die Schranke -
    der Champion bleibt."""
    ist, champion, kandidat = _fall_a(peak=2000.0)
    neu = compare_point_forecasts(ist, champion, kandidat, TZ, **PV_OPTIONEN)
    assert neu["level_adjusted"]["improvement_percent"] > 50
    assert neu["promote"] is False and neu["status"] == "held"
    assert "Bias" in neu["reason"]
    assert neu["bias_increase_w"] > 75.0


def test_grob_falsches_niveau_haelt_den_champion():
    """(b) Gleiche Form, grob falsches Niveau: niveaubereinigt sind beide
    gleich gut - kein Vorsprung, der Champion bleibt. Ist die Form des
    Kandidaten sogar etwas besser, haelt ihn die Bias-Schranke auf; ohne sie
    die Obergrenze fuer den unskalierten WAPE."""
    index, ist = _truebphase(peak=4000.0)
    champion = _klar(index, 4000.0) * 0.66
    gleich = champion * 1.6
    entscheid = compare_point_forecasts(
        ist, champion, gleich, TZ, **PV_OPTIONEN)
    assert entscheid["promote"] is False
    assert abs(entscheid["level_adjusted"]["improvement_percent"]) < 1e-6

    schief = _klar(index, 4000.0, versatz_h=0.5) * 0.66
    besser_aber_zu_hoch = _klar(index, 4000.0) * 0.66 * 1.6
    entscheid = compare_point_forecasts(
        ist, schief, besser_aber_zu_hoch, TZ, **PV_OPTIONEN)
    assert entscheid["level_adjusted"]["improvement_percent"] > 1.0
    assert entscheid["promote"] is False and "Bias" in entscheid["reason"]

    ohne_bias = compare_point_forecasts(
        ist, schief, besser_aber_zu_hoch, TZ,
        max_bias_increase_w=float("inf"), **PV_OPTIONEN)
    assert ohne_bias["promote"] is False
    assert "unskalierter WAPE" in ohne_bias["reason"]
    assert ohne_bias["raw_wape_increase_pct"] > 5.0


def test_tage_fast_ohne_energie_destabilisieren_nicht():
    """(c) Schneetage (Ist fast null) und ein Tag fast ohne Prognose bekommen
    keinen eigenen Faktor; Entscheidung und Kennzahlen bleiben stabil."""
    ist, champion, kandidat = _fall_a()
    basis = compare_point_forecasts(ist, champion, kandidat, TZ, **PV_OPTIONEN)

    tag = pd.Index(ist.index.tz_convert(TZ).date)
    tage = sorted(set(tag))
    schnee = np.isin(tag, tage[3:5])           # Ist bricht weg
    leer = np.isin(tag, tage[8:9])             # Prognosen fast null
    ist2, champion2, kandidat2 = ist.copy(), champion.copy(), kandidat.copy()
    ist2[schnee] = 0.5
    champion2[leer] = 0.01
    kandidat2[leer] = 0.02

    skaliert, info = level_adjust(
        ist2, {"champion": champion2, "challenger": kandidat2}, TZ)
    assert info["days"] == 14 and info["days_frame_factor"] == 3
    for name, reihe in skaliert.items():
        assert np.isfinite(reihe.to_numpy()).all()
        faktor = (reihe / {"champion": champion2,
                           "challenger": kandidat2}[name]).dropna()
        assert faktor.min() >= LEVEL_FACTOR_BOUNDS[0] - 1e-9
        assert faktor.max() <= LEVEL_FACTOR_BOUNDS[1] + 1e-9

    gestoert = compare_point_forecasts(
        ist2, champion2, kandidat2, TZ, max_bias_increase_w=float("inf"),
        max_segment_degradation_pct=1e9, level_adjusted=True)
    assert gestoert["level_adjusted"]["days_frame_factor"] == 3
    # die Form-Aussage kippt nicht: der Kandidat bleibt klar vorn
    assert gestoert["level_adjusted"]["improvement_percent"] > 20
    assert basis["level_adjusted"]["improvement_percent"] > 20
    assert np.isfinite(gestoert["level_adjusted"]["champion"]["wape_pct"])


def test_ein_tag_mit_winziger_prognose_bekommt_keinen_riesenfaktor():
    """(c) Summe Ist / Summe Prognose waere an so einem Tag 1000 - der Tag
    laeuft mit dem Rahmenfaktor, nichts ueberschreitet die Kappung."""
    index, ist = _truebphase()
    prognose = _klar(index, 500.0)
    tag = pd.Index(index.tz_convert(TZ).date)
    winzig = tag == sorted(set(tag))[6]
    prognose[winzig] = prognose[winzig] * 0.001
    skaliert, info = level_adjust(ist, {"p": prognose}, TZ)
    faktor = skaliert["p"] / prognose
    assert info["days_frame_factor"] == 1
    assert np.allclose(faktor[winzig], info["series"]["p"]["frame_factor"],
                       atol=1e-3)
    assert faktor.max() <= LEVEL_FACTOR_BOUNDS[1]


def test_rahmenfaktor_und_tagesfaktor_behandeln_beide_reihen_gleich():
    """Ein Tag ist nur dann eigenstaendig, wenn er das fuer BEIDE Reihen ist."""
    index, ist = _truebphase()
    a = _klar(index, 500.0)
    b = _klar(index, 500.0)
    tag = pd.Index(index.tz_convert(TZ).date)
    b[tag == sorted(set(tag))[2]] *= 0.01      # nur Reihe b ist dort leer
    skaliert, info = level_adjust(ist, {"a": a, "b": b}, TZ)
    assert info["days_frame_factor"] == 1
    fa = (skaliert["a"] / a)[tag == sorted(set(tag))[2]]
    assert fa.iloc[0] == pytest.approx(info["series"]["a"]["frame_factor"],
                                       abs=1e-3)
    frame, info_frame = level_adjust(ist, {"a": a}, TZ, method="frame")
    assert info_frame["days_own_factor"] == 0
    assert float(frame["a"].sum()) == pytest.approx(float(ist.sum()))
    with pytest.raises(ValueError):
        level_adjust(ist, {"a": a}, TZ, method="stunde")


def test_lastvergleich_bleibt_unskaliert():
    """(d) Ohne ``level_adjusted`` aendert sich nichts: ein Lastprofil, das nur
    das Niveau korrigiert, gewinnt weiterhin - niveaubereinigt waere es vom
    Champion nicht zu unterscheiden."""
    index = pd.date_range("2026-07-01", periods=14 * 96, freq="15min", tz=TZ)
    stunde = index.hour + index.minute / 60.0
    ist = pd.Series(600.0 + 500.0 * ((stunde >= 17) & (stunde < 22)),
                    index=index)
    champion, kandidat = ist * 1.20, ist * 1.03
    last = compare_point_forecasts(ist, champion, kandidat, TZ)
    assert last["promote"] is True
    assert last["improvement_percent"] > 50.0
    assert set(last) == {
        "n", "champion", "challenger", "promote", "status", "reason",
        "improvement_percent", "worst_segment_degradation_pct",
        "segments_checked", "segment_guard", "segments"}
    bereinigt = compare_point_forecasts(
        ist, champion, kandidat, TZ, level_adjusted=True)
    assert bereinigt["promote"] is False

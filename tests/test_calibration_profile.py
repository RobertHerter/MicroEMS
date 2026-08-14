"""Korrekturprofile je PV-Quellgruppe."""
from ems import calibration

def test_profile_for_source_picks_the_matching_entry():
    profil = {"pv_global": 1.1,
              "pv_sources": {"solcast": {"pv_global": 1.2},
                             "pvlib": {"pv_global": 0.8}}}
    assert calibration.pv_profile_for_source(profil, "pvlib")["pv_global"] == 0.8
    assert calibration.pv_profile_for_source(
        profil, "solcast")["pv_global"] == 1.2


def test_profile_for_source_refuses_a_foreign_profile():
    """Fremdes Profil ist schlechter als gar keins: pvlib mit Solcasts
    Faktoren kam auf WAPE 22,2 statt 16,0 roh."""
    # neues Format, aber die Quelle fehlt darin -> keine Korrektur
    profil = {"pv_global": 1.3, "pv_sources": {"solcast": {"pv_global": 1.2}}}
    assert calibration.pv_profile_for_source(profil, "pvlib") is None
    # altes Format: die flache Ebene gehoert der damals aktiven Quelle
    alt = {"pv_global": 1.3, "pv_hour": {12: 1.4}}
    assert calibration.pv_profile_for_source(alt, "pvlib", "solcast") is None
    assert calibration.pv_profile_for_source(alt, "solcast", "solcast") is alt


def test_source_summary_carries_metrics_and_promotion():
    """Der Bericht muss je Quellgruppe eine Guete ausweisen - sonst ist die
    Schattenquelle nur ueber einen Handaufruf beurteilbar."""
    import kalibrierung as K
    res = {"forecast_source": "lokal (pvlib)",
           "fit_metrics": {"n": 210, "nmae_pct": 16.2, "rmse_W": 900.0,
                           "bias_W": 15.9, "corr": 0.94,
                           "scale_actual_over_pred": 0.9874},
           "hourly": {12: 0.98}, "monthly": {8: 0.99}}
    s = K._pv_source_summary(res, "Schattenmodell",
                             {"promote": True, "status": "promoted",
                              "reason": "besser", "n": 210})
    assert s["rolle"] == "Schattenmodell" and s["n"] == 210
    assert s["nmae_pct"] == 16.2 and s["scale_actual_over_pred"] == 0.9874
    assert s["promotion"]["promote"] is True
    assert s["monthly"] == {8: 0.99}


def _reihen(stunden_slots, faktor):
    """(pred, actual) auf den gegebenen Zeitpunkten mit festem Verhaeltnis."""
    import pandas as pd
    idx = pd.DatetimeIndex(stunden_slots)
    pred = pd.Series(1000.0, index=idx)
    return pred, pred * faktor


def test_thin_profile_cells_are_dropped_instead_of_applied():
    """Ohne Gate bestimmte ein einziger Slot einen voll wirksamen Faktor - beim
    Monatswechsel also der erste Tag des neuen Monats, genau dort, wo das Profil
    saisonal umschwenken soll und die Datenlage am duennsten ist."""
    import pandas as pd
    import kalibrierung as K
    from ems.quality import MIN_SAMPLES
    tz = "Europe/Berlin"
    dick = [pd.Timestamp(f"2026-07-{tag:02d} 12:{minute:02d}", tz=tz)
            for tag in (10, 11, 12, 13) for minute in (0, 15, 30, 45)]
    duenn = [pd.Timestamp(f"2026-08-01 12:{minute:02d}", tz=tz)
             for minute in (0, 15, 30, 45)]
    assert len(dick) >= MIN_SAMPLES["profile_cell"] > len(duenn)

    pred_d, akt_d = _reihen(dick, 0.9)
    pred_t, akt_t = _reihen(duenn, 0.3)      # extremer Wert auf wenigen Slots
    pred = pd.concat([pred_d, pred_t])
    akt = pd.concat([akt_d, akt_t])

    tab = K._month_hour_table(akt, pred, tz)
    assert tab.get(7, {}).get(12) == 0.9
    assert 8 not in tab, "duenne Monatszelle darf nicht angewandt werden"

    # gleiche Regel fuer die reinen Stunden- und Monatstabellen
    monate = K._factor_table(akt, pred, lambda i: i.tz_convert(tz).month)
    assert 7 in monate and 8 not in monate

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

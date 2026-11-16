import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticForecastQualityTest(unittest.TestCase):
    def test_dashboard_contains_midnight_forecast_quality_section(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="quality-ranking"', html)
        self.assertIn('id="quality-days"', html)
        self.assertIn("function renderQuality()", app)
        self.assertIn("renderQuality();", app)
        self.assertIn("SOC", html)
        self.assertIn("Netzbezug", html)
        self.assertIn("gridImportMaeKw", app)
        self.assertIn("socMaePp", app)
        self.assertNotIn("PV und Hauslast abgeschlossener Tage", html)

    def test_empty_native_history_is_explained_instead_of_scored_as_zero(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("Mitternachtsstände werden gesammelt", app)
        self.assertNotIn("distanceScorePct || 0", app)
        self.assertIn("value !== null && value !== undefined", app)


if __name__ == "__main__":
    unittest.main()

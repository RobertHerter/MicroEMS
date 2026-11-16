import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticTimelineAlignmentTest(unittest.TestCase):
    def test_graph_and_intent_strip_share_the_same_time_domain(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        self.assertGreaterEqual(app.count("timelineDomain(systems)"), 2)
        self.assertIn("Date.parse(point.t) - timeMin", app)

    def test_intent_strip_uses_the_graph_plot_margins(self):
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("--intent-label-width", app)
        self.assertIn("--intent-label-width", css)
        self.assertNotIn("grid-template-columns: repeat(192, 1fr)", css)


if __name__ == "__main__":
    unittest.main()

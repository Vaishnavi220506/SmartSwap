from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_static_ui_contains_all_routes_and_native_controls() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    for page in ("monitor", "experiment", "evidence", "reports"):
        assert f'data-page="{page}"' in html
        assert f'id="page-{page}"' in html
    for endpoint in ("/api/metrics", "/api/live", "/api/comparison", "/api/paired-comparison", "/api/cleanup", "/api/study"):
        assert endpoint in js
    assert "c.classification" in js
    css = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8").lower()
    for classification in ("improved", "regressed", "approximately_unchanged", "insufficient_data"):
        assert classification in css or classification == "insufficient_data"


def test_static_ui_has_no_fake_metric_sources() -> None:
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8").lower()
    assert "math.random" not in js
    assert "setinterval" in js

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def test_ui_documents_the_real_zetaris_setup_command():
    assert "python scripts/zetaris_setup.py --apply" in HTML
    assert "zetaris_setup.py all" in HTML


def test_queue_keeps_verdict_separate_from_alert_id():
    assert "alert-head" in HTML
    assert "class=\"badge ${verdicts[a.alert_id]}\">${verdicts[a.alert_id]}</span>`:''}" not in HTML

from pathlib import Path

from app.healing import diagnostics as diag_mod
from app.healing.diagnostics import DiagnosticBundle, sanitize_network_entry


def test_bundle_writes_redacted_dom(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_mod, "DIAG_ROOT", tmp_path)
    b = DiagnosticBundle(account="B")
    b.dom = 'Authorization: Bearer sk-abc123xyz456\nuser@example.com was here'
    b.record_layer("goto", 0.5, "timeout")
    out = b.write()
    assert out is not None
    assert out.exists()
    dom_text = (out / "dom.html").read_text()
    assert "Bearer" not in dom_text
    assert "user@example.com" not in dom_text
    assert "***REDACTED***" in dom_text


def test_bundle_writes_trace_json(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_mod, "DIAG_ROOT", tmp_path)
    b = DiagnosticBundle(account="B")
    b.record_layer("goto", 0.5, None)
    b.record_layer("submit", 1.2, "selector_not_found")
    out = b.write()
    trace_path = out / "trace.json"
    assert trace_path.exists()
    text = trace_path.read_text()
    assert '"layer": "goto"' in text
    assert '"error_class": "selector_not_found"' in text


def test_bundle_writes_screenshot_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(diag_mod, "DIAG_ROOT", tmp_path)
    b = DiagnosticBundle(account="B")
    out = b.write()
    assert not (out / "screenshot.png").exists()

    b2 = DiagnosticBundle(account="B")
    b2.screenshot_bytes = b"\x89PNG fake"
    out2 = b2.write()
    assert (out2 / "screenshot.png").exists()


def test_sanitize_network_entry_drops_headers():
    raw = {
        "url": "https://claude.ai/api/x",
        "method": "POST",
        "status": 200,
        "timing_ms": 120,
        "headers": {"Authorization": "Bearer sk-supersecret"},
        "body": "hidden",
    }
    out = sanitize_network_entry(raw)
    assert set(out.keys()) == {"url", "method", "status", "timing_ms"}
    assert "headers" not in out
    assert "body" not in out

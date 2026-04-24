import base64
import json

from app.healing import l2_env


def test_l2_returns_false_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_ACCOUNT_B_SESSION_TOKEN", raising=False)
    assert l2_env.restore_from_env() is False


def test_l2_rejects_invalid_base64(monkeypatch):
    monkeypatch.setenv("CLAUDE_ACCOUNT_B_SESSION_TOKEN", "!!!not-base64!!!")
    assert l2_env.restore_from_env() is False


def test_l2_rejects_invalid_json_payload(monkeypatch):
    monkeypatch.setenv(
        "CLAUDE_ACCOUNT_B_SESSION_TOKEN",
        base64.b64encode(b"this is not json").decode(),
    )
    assert l2_env.restore_from_env() is False


def test_l2_writes_credentials_on_valid_payload(monkeypatch, tmp_path):
    fake_path = tmp_path / "claude-b" / "credentials.json"
    monkeypatch.setattr(l2_env, "ACCOUNT_B_LIVE", fake_path)
    payload = {"access_token": "xyz", "refresh_token": "abc"}
    monkeypatch.setenv(
        "CLAUDE_ACCOUNT_B_SESSION_TOKEN",
        base64.b64encode(json.dumps(payload).encode()).decode(),
    )
    assert l2_env.restore_from_env() is True
    assert json.loads(fake_path.read_text()) == payload

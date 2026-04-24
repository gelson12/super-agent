from app.redact import redact


def test_redacts_openai_style_key():
    assert "***REDACTED***" in redact("token=sk-abcdef1234567890abcdef12345678")


def test_redacts_bearer_header():
    assert "***REDACTED***" in redact("Authorization: Bearer eyJabc.def.ghi")


def test_redacts_session_token_json():
    out = redact('{"session_token": "long-opaque-value-here"}')
    assert "long-opaque-value-here" not in out
    assert "***REDACTED***" in out


def test_redacts_email_address():
    out = redact("user contacted gelson_m@hotmail.com about the issue")
    assert "gelson_m@hotmail.com" not in out


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert jwt not in redact(f"authed with {jwt}")


def test_passthrough_safe_text():
    assert redact("no secrets here") == "no secrets here"


def test_empty_string():
    assert redact("") == ""

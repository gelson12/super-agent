import logging
import re

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"Bearer [A-Za-z0-9._\-]+"),
    re.compile(r'"session_token"\s*:\s*"[^"]+"'),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
]
_REPLACEMENT = "***REDACTED***"


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(_REPLACEMENT, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def install_root_filter() -> None:
    logging.getLogger().addFilter(RedactFilter())

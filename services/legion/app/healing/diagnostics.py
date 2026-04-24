"""
Redaction-safe diagnostics writer for L4/L5 failures. Outputs to
/workspace/legion/diag/<ts>_<account>/ so each healing attempt is isolated
and the runtime redactor (app.redact) scrubs any secrets before they hit
the artefact files.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.redact import redact

log = logging.getLogger("legion.healing.diag")

DIAG_ROOT = Path("/workspace/legion/diag")


@dataclass
class DiagnosticBundle:
    account: str
    started_at: float = field(default_factory=time.time)
    layer_history: list[dict[str, Any]] = field(default_factory=list)
    dom: str = ""
    console: list[str] = field(default_factory=list)
    screenshot_bytes: bytes = b""
    network: list[dict[str, Any]] = field(default_factory=list)

    def record_layer(self, layer: str, duration_s: float, error_class: str | None) -> None:
        self.layer_history.append({
            "layer": layer,
            "duration_s": round(duration_s, 3),
            "error_class": error_class,
        })

    def write(self) -> Path | None:
        ts = int(self.started_at)
        path = DIAG_ROOT / f"{ts}_{self.account}"
        try:
            path.mkdir(parents=True, exist_ok=True)
            (path / "trace.json").write_text(json.dumps({
                "account": self.account,
                "started_at": self.started_at,
                "layers": self.layer_history,
            }, indent=2))
            (path / "dom.html").write_text(redact(self.dom))
            (path / "console.log").write_text(redact("\n".join(self.console)))
            if self.screenshot_bytes:
                (path / "screenshot.png").write_bytes(self.screenshot_bytes)
            (path / "network.har").write_text(json.dumps(self.network))
            return path
        except OSError as exc:
            log.warning("diag write failed at %s: %s", path, type(exc).__name__)
            return None


def sanitize_network_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only URL/method/status/timing — never headers, cookies, or body."""
    return {
        "url": entry.get("url"),
        "method": entry.get("method"),
        "status": entry.get("status"),
        "timing_ms": entry.get("timing_ms"),
    }

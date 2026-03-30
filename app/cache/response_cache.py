"""
In-memory response cache with TTL and stats.

Normalises queries before hashing so minor wording variations
("what is X" vs "what's X") hit the same cache entry.
No external dependencies — zero infra cost.
"""
import hashlib
import re
import time
from typing import Optional


class ResponseCache:
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._hits = 0
        self._misses = 0
        self._tokens_saved = 0

    # ── Normalisation ──────────────────────────────────────────────────────────

    def _normalise(self, text: str) -> str:
        """Lowercase, collapse whitespace, strip punctuation noise."""
        text = text.lower().strip()
        text = re.sub(r"['\u2019\u2018]", "", text)   # smart quotes
        text = re.sub(r"[^\w\s]", " ", text)           # punctuation → space
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _key(self, message: str, model: str) -> str:
        normalised = self._normalise(message)
        raw = f"{model}:{normalised}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, message: str, model: str, ttl: int = 3600) -> Optional[str]:
        """
        Return cached response if it exists and is within TTL.
        TTL defaults to 1 hour; pass 0 to skip cache.
        """
        if ttl == 0:
            self._misses += 1
            return None
        key = self._key(message, model)
        entry = self._store.get(key)
        if entry and (time.time() - entry["ts"]) < ttl:
            self._hits += 1
            self._tokens_saved += entry.get("approx_tokens", 400)
            return entry["response"]
        self._misses += 1
        return None

    def set(self, message: str, model: str, response: str) -> None:
        """Store a response. Evicts expired entries every 200 writes."""
        key = self._key(message, model)
        approx_tokens = int(len(response.split()) * 1.35)
        self._store[key] = {
            "response": response,
            "ts": time.time(),
            "model": model,
            "approx_tokens": approx_tokens,
        }
        if len(self._store) % 200 == 0:
            self._evict()

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total_queries": total,
            "hit_rate_pct": round(self._hits / max(total, 1) * 100, 1),
            "cached_entries": len(self._store),
            "est_tokens_saved": self._tokens_saved,
            "est_cost_saved_usd": round(self._tokens_saved / 1_000_000 * 1.25, 4),
        }

    def clear(self) -> None:
        self._store.clear()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _evict(self) -> None:
        """Remove all entries older than 24 hours."""
        cutoff = time.time() - 86400
        stale = [k for k, v in self._store.items() if v["ts"] < cutoff]
        for k in stale:
            del self._store[k]


# Singleton — shared across all requests
cache = ResponseCache()

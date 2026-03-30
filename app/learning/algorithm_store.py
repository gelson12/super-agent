"""
Algorithm Store — loads self-built algorithms from the GitHub repo
'gelson12/super-agent-algorithms' and makes them callable at runtime.

The store refreshes its algorithm registry every REFRESH_INTERVAL_SECS
(default 3600 = 1 hour) so new algorithms committed by the builder
are picked up without a container restart.

Usage:
    from app.learning.algorithm_store import algorithm_store

    algo = algorithm_store.get_algorithm("routing_heuristic")
    if algo:
        model = algo.run(category="code/math", complexity=4)

Each algorithm exposes a .run(**kwargs) method that executes the
generated Python code in a restricted namespace.
"""
import base64
import time
import types
import threading
from typing import Any, Optional

REFRESH_INTERVAL_SECS = 3600  # re-fetch from GitHub every hour
ALGO_REPO = "super-agent-algorithms"
ALGO_BRANCH = "main"
ALGO_DIR = "algorithms"

# Known algorithm filenames → human-readable name
_KNOWN_ALGOS = {
    "routing_heuristic.py": "routing_heuristic",
    "complexity_predictor.py": "complexity_predictor",
    "error_recovery.py": "error_recovery",
}


class LoadedAlgorithm:
    """A compiled algorithm loaded from a .py file in the GitHub repo."""

    def __init__(self, name: str, code: str) -> None:
        self.name = name
        self._module = types.ModuleType(f"algo_{name}")
        try:
            exec(compile(code, f"<algo:{name}>", "exec"), self._module.__dict__)
            self._ok = True
        except Exception as e:
            self._ok = False
            self._error = str(e)

    @property
    def ok(self) -> bool:
        return self._ok

    def run(self, fn_name: Optional[str] = None, **kwargs) -> Any:
        """
        Execute the algorithm's main function.

        Args:
            fn_name: Specific function to call. If None, uses the first
                     public callable in the module.
            **kwargs: Arguments forwarded to the function.

        Returns:
            Whatever the function returns.

        Raises:
            RuntimeError if the algorithm failed to load or fn not found.
        """
        if not self._ok:
            raise RuntimeError(f"Algorithm '{self.name}' failed to load: {self._error}")

        if fn_name:
            fn = getattr(self._module, fn_name, None)
            if fn is None or not callable(fn):
                raise RuntimeError(f"Function '{fn_name}' not found in algorithm '{self.name}'")
            return fn(**kwargs)

        # Auto-detect: first public callable that is not a class
        for attr in dir(self._module):
            if attr.startswith("_"):
                continue
            obj = getattr(self._module, attr)
            if callable(obj) and not isinstance(obj, type):
                return obj(**kwargs)

        raise RuntimeError(f"No callable function found in algorithm '{self.name}'")


class AlgorithmStore:
    """
    Registry of self-built algorithms loaded from the GitHub repo.
    Thread-safe. Refreshes automatically once per hour.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._algorithms: dict[str, LoadedAlgorithm] = {}
        self._last_refresh: float = 0.0
        self._refresh()  # eager load on startup

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Fetch algorithm files from GitHub and (re)compile them."""
        try:
            from ..tools.github_tools import _client
            from github import GithubException

            repo = _client().get_repo(f"gelson12/{ALGO_REPO}")
            loaded: dict[str, LoadedAlgorithm] = {}

            try:
                contents = repo.get_contents(ALGO_DIR, ref=ALGO_BRANCH)
            except GithubException:
                contents = []

            for item in contents:
                if item.type != "file" or not item.name.endswith(".py"):
                    continue
                algo_name = _KNOWN_ALGOS.get(item.name, item.name[:-3])
                try:
                    code = base64.b64decode(item.content).decode("utf-8")
                    loaded[algo_name] = LoadedAlgorithm(algo_name, code)
                except Exception:
                    pass

            with self._lock:
                self._algorithms = loaded
                self._last_refresh = time.time()

        except Exception:
            # Non-fatal — keep using whatever is already loaded
            with self._lock:
                self._last_refresh = time.time()

    def _maybe_refresh(self) -> None:
        if time.time() - self._last_refresh > REFRESH_INTERVAL_SECS:
            self._refresh()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_algorithm(self, name: str) -> Optional[LoadedAlgorithm]:
        """
        Return a LoadedAlgorithm by name, or None if not available.
        Triggers a background refresh if the cache is stale.
        """
        self._maybe_refresh()
        with self._lock:
            return self._algorithms.get(name)

    def list_algorithms(self) -> list[dict]:
        """
        Return a list of dicts describing loaded algorithms.
        Safe for JSON serialisation.
        """
        self._maybe_refresh()
        with self._lock:
            return [
                {
                    "name": name,
                    "ok": algo.ok,
                    "last_refreshed_ts": self._last_refresh,
                }
                for name, algo in self._algorithms.items()
            ]

    def run(self, name: str, fn_name: Optional[str] = None, **kwargs) -> Any:
        """
        Convenience method — look up and run an algorithm in one call.

        Raises RuntimeError if the algorithm does not exist or fails.
        """
        algo = self.get_algorithm(name)
        if algo is None:
            raise RuntimeError(
                f"Algorithm '{name}' not found. "
                f"Available: {list(self._algorithms.keys())}"
            )
        return algo.run(fn_name=fn_name, **kwargs)

    def status(self) -> dict:
        """Return store status for diagnostics."""
        with self._lock:
            return {
                "algorithm_count": len(self._algorithms),
                "algorithms": [n for n in self._algorithms],
                "last_refresh_ts": self._last_refresh,
                "refresh_interval_secs": REFRESH_INTERVAL_SECS,
            }


# Singleton
algorithm_store = AlgorithmStore()

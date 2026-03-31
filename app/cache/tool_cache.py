"""
TTL cache decorator for expensive read-only tool calls.

Usage:
    from ..cache.tool_cache import cached_tool

    @tool
    @cached_tool(ttl=300)
    def my_expensive_tool(arg: str) -> str:
        ...

Cached results are returned without hitting the external API again
until the TTL (in seconds) expires. The cache is in-process memory —
it resets on container restart, which is the correct behaviour for
live data like logs and workflow lists.
"""
import functools
import time

_store: dict[str, tuple[str, float]] = {}


def cached_tool(ttl: int = 300):
    """
    Decorator: cache the return value of a tool function for `ttl` seconds.
    Keyed on function name + all arguments.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            if key in _store:
                value, ts = _store[key]
                if time.time() - ts < ttl:
                    return value  # return cached value transparently
            result = fn(*args, **kwargs)
            # Only cache successful results (not error strings)
            if isinstance(result, str) and not result.startswith("["):
                _store[key] = (result, time.time())
            return result
        return wrapper
    return decorator

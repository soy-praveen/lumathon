"""Best-effort tracing around agent runs.

Uses the Neatlogs SDK when NEATLOGS_API_KEY is set. Without the key, or if the
SDK is missing or misbehaves, tracing is a silent no-op so the close never
fails because of observability.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

_initialized = False


def _init_neatlogs(api_key: str) -> bool:
    global _initialized
    if _initialized:
        return True
    try:
        import neatlogs

        neatlogs.init(api_key)
        _initialized = True
        return True
    except Exception:
        return False


@contextmanager
def traced_run(name: str) -> Iterator[None]:
    """Trace a named agent run. No-op when the key or SDK is absent."""
    api_key = os.environ.get("NEATLOGS_API_KEY")
    if not api_key or not _init_neatlogs(api_key):
        yield
        return
    try:
        import neatlogs

        add_tags = getattr(neatlogs, "add_tags", None)
        if callable(add_tags):
            add_tags([name])
    except Exception:
        pass
    yield

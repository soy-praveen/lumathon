"""Best-effort tracing around agent runs.

Uses the Neatlogs SDK when NEATLOGS_API_KEY is set. Without the key, or if the
SDK is missing or misbehaves, every helper here is a silent no-op so the close
never fails because of observability.

Span model (Neatlogs / OpenInference kinds):
- traced_run: one WORKFLOW trace per close run
- span("TOOL"): each deterministic engine (recon, accrual, flux, anomaly)
- span("CHAIN"): LLM calls made through sentinel.llm
- log: timestamped facts inside the active span (counts, decisions)
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

WORKFLOW_NAME = "ledger-sentinel"

_initialized = False
_broken = False

F = TypeVar("F", bound=Callable[..., Any])


def _enabled() -> bool:
    """True when a key is present and the SDK initialised without error."""
    global _initialized, _broken
    if _broken:
        return False
    api_key = os.environ.get("NEATLOGS_API_KEY")
    if not api_key:
        return False
    if _initialized:
        return True
    try:
        import neatlogs

        neatlogs.init(api_key=api_key, workflow_name=WORKFLOW_NAME)
        _initialized = True
        return True
    except Exception:
        _broken = True
        return False


@contextmanager
def traced_run(name: str) -> Iterator[None]:
    """Trace a named agent run as a top-level workflow. No-op when disabled."""
    if not _enabled():
        yield
        return
    try:
        import neatlogs
        from neatlogs import init as neatlogs_init

        # Neatlogs keeps its own tracer provider rather than installing the
        # OpenTelemetry global one, so the root span must come from it.
        provider = getattr(neatlogs_init, "_tracer_provider", None)
        if provider is None:
            from opentelemetry import trace as otel_trace

            provider = otel_trace.get_tracer_provider()
        tracer = provider.get_tracer(WORKFLOW_NAME)
        attributes = {
            "openinference.span.kind": "WORKFLOW",
            "neatlogs.span.kind": "workflow",
            "workflow.name": WORKFLOW_NAME,
        }
        with tracer.start_as_current_span(name, attributes=attributes):
            yield
        try:
            neatlogs.flush(timeout_millis=5000)
        except Exception:
            pass
    except Exception:
        # If the SDK itself raises on entry, still run the body untraced.
        yield


def span(kind: str, name: str | None = None, **options: Any) -> Callable[[F], F]:
    """Decorate a function as a span of the given kind. No-op when disabled.

    The decision is made per call, so setting the key after import still works.
    """

    def decorate(fn: F) -> F:
        traced: Callable[..., Any] | None = None

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal traced
            if not _enabled():
                return fn(*args, **kwargs)
            if traced is None:
                try:
                    import neatlogs

                    traced = neatlogs.span(kind, name=name or fn.__name__, **options)(fn)
                except Exception:
                    traced = fn
            return traced(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def log(message: str, **data: Any) -> None:
    """Record a timestamped fact inside the active span. No-op when disabled."""
    if not _enabled():
        return
    try:
        import neatlogs

        neatlogs.log(message, **data)
    except Exception:
        pass

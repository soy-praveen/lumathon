import sys

import pytest

from sentinel import tracing
from sentinel.tracing import log, span, traced_run


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(tracing, "_initialized", False)
    monkeypatch.setattr(tracing, "_broken", False)
    yield


def test_traced_run_noop_without_key(monkeypatch):
    monkeypatch.delenv("NEATLOGS_API_KEY", raising=False)
    ran = False
    with traced_run("close-2026-01"):
        ran = True
    assert ran


def test_traced_run_survives_missing_sdk(monkeypatch):
    monkeypatch.setenv("NEATLOGS_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "neatlogs", None)
    ran = False
    with traced_run("close-2026-01"):
        ran = True
    assert ran
    assert tracing._broken is True


def test_span_and_log_are_transparent_without_key(monkeypatch):
    monkeypatch.delenv("NEATLOGS_API_KEY", raising=False)

    @span("TOOL", name="adder")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5
    assert add.__name__ == "add"
    log("nothing happens {n}", n=1)


def test_span_falls_back_when_sdk_missing(monkeypatch):
    monkeypatch.setenv("NEATLOGS_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "neatlogs", None)

    @span("CHAIN")
    def double(x: int) -> int:
        return 2 * x

    assert double(4) == 8

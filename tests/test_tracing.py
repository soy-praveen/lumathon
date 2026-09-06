from sentinel.tracing import traced_run


def test_traced_run_noop_without_key(monkeypatch):
    monkeypatch.delenv("NEATLOGS_API_KEY", raising=False)
    ran = False
    with traced_run("close-2026-01"):
        ran = True
    assert ran


def test_traced_run_survives_missing_sdk(monkeypatch):
    monkeypatch.setenv("NEATLOGS_API_KEY", "test-key")
    ran = False
    with traced_run("close-2026-01"):
        ran = True
    assert ran

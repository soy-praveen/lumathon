import json
import subprocess

import pytest
from pydantic import BaseModel

from sentinel import llm
from sentinel.llm import LLMError, complete, complete_json


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def fake_run(stdout: str, returncode: int = 0, stderr: str = ""):
    def _run(cmd, **kwargs):
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _run


def test_cli_fallback_returns_result(monkeypatch):
    payload = json.dumps({"type": "result", "is_error": False, "result": "hello from cli"})
    monkeypatch.setattr(llm.subprocess, "run", fake_run(payload))
    assert complete("say hello") == "hello from cli"


def test_cli_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", fake_run("", returncode=1, stderr="boom"))
    with pytest.raises(LLMError):
        complete("say hello")


def test_cli_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(llm.subprocess, "run", fake_run("not json"))
    with pytest.raises(LLMError):
        complete("say hello")


def test_cli_error_payload_raises(monkeypatch):
    payload = json.dumps({"type": "result", "is_error": True, "result": "overloaded"})
    monkeypatch.setattr(llm.subprocess, "run", fake_run(payload))
    with pytest.raises(LLMError):
        complete("say hello")


def test_cli_missing_binary_raises(monkeypatch):
    def _run(cmd, **kwargs):
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(llm.subprocess, "run", _run)
    with pytest.raises(LLMError):
        complete("say hello")


class Verdict(BaseModel):
    label: str
    score: float


def test_complete_json_parses_model(monkeypatch):
    inner = json.dumps({"label": "match", "score": 0.9})
    payload = json.dumps({"type": "result", "is_error": False, "result": inner})
    monkeypatch.setattr(llm.subprocess, "run", fake_run(payload))
    verdict = complete_json("classify this", Verdict)
    assert isinstance(verdict, Verdict)
    assert verdict.label == "match"


def test_complete_json_strips_code_fences(monkeypatch):
    inner = "```json\n" + json.dumps({"label": "match", "score": 0.9}) + "\n```"
    payload = json.dumps({"type": "result", "is_error": False, "result": inner})
    monkeypatch.setattr(llm.subprocess, "run", fake_run(payload))
    verdict = complete_json("classify this", Verdict)
    assert verdict.score == 0.9


def test_complete_json_invalid_shape_raises(monkeypatch):
    payload = json.dumps({"type": "result", "is_error": False, "result": '{"nope": true}'})
    monkeypatch.setattr(llm.subprocess, "run", fake_run(payload))
    with pytest.raises(LLMError):
        complete_json("classify this", Verdict)

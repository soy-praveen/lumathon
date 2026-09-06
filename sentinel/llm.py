"""The only module allowed to call a language model.

Uses the Anthropic SDK when ANTHROPIC_API_KEY is set, otherwise shells out to
the local claude CLI in headless mode. Engines call complete/complete_json for
ambiguity resolution, narrative, and rule distillation, never a model directly.
"""

from __future__ import annotations

import json
import os
import subprocess

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "claude-sonnet-5"
CLI_TIMEOUT_SECONDS = 300


class LLMError(Exception):
    """Raised when the model call fails or returns unusable output."""


def complete(prompt: str, system: str | None = None, model: str | None = None) -> str:
    """Return the model's text completion for the prompt."""
    model = model or DEFAULT_MODEL
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _complete_sdk(prompt, system, model)
    return _complete_cli(prompt, system, model)


def complete_json(
    prompt: str,
    schema: type[BaseModel],
    system: str | None = None,
    model: str | None = None,
) -> BaseModel:
    """Return a validated instance of `schema` parsed from the model's output."""
    json_schema = json.dumps(schema.model_json_schema())
    wrapped = (
        f"{prompt}\n\n"
        "Respond with a single JSON object matching this JSON schema, "
        f"with no surrounding text or code fences:\n{json_schema}"
    )
    raw = complete(wrapped, system=system, model=model)
    try:
        return schema.model_validate_json(_strip_fences(raw))
    except ValidationError as exc:
        raise LLMError(f"model output did not match {schema.__name__}: {exc}") from exc


def _complete_sdk(prompt: str, system: str | None, model: str) -> str:
    import anthropic

    kwargs: dict = {}
    if system is not None:
        kwargs["system"] = system
    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
    except Exception as exc:
        raise LLMError(f"Anthropic SDK call failed: {exc}") from exc
    text = "".join(block.text for block in message.content if block.type == "text")
    if not text:
        raise LLMError("Anthropic SDK returned no text content")
    return text


def _complete_cli(prompt: str, system: str | None, model: str) -> str:
    cmd = ["claude", "-p", "--output-format", "json", "--model", model]
    if system is not None:
        cmd += ["--append-system-prompt", system]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMError(f"claude CLI unavailable or timed out: {exc}") from exc
    if proc.returncode != 0:
        raise LLMError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"claude CLI returned invalid JSON: {exc}") from exc
    if payload.get("is_error"):
        raise LLMError(f"claude CLI reported an error: {payload.get('result')}")
    result = payload.get("result")
    if not isinstance(result, str):
        raise LLMError("claude CLI JSON payload has no text result")
    return result


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()

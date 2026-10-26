"""Model-agnostic client: any OpenAI-compatible endpoint, one retry on bad output.

The completion layer is injected, so the retry and validation logic is testable
without a network or an API key.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import ValidationError

from gira_iso.nl.prompt import SYSTEM_PROMPT, user_message
from gira_iso.nl.schema import Interpretation

DEFAULT_BASE_URL = "https://api.llmapi.ai/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"


class InterpretError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    timeout: float = 90.0


@dataclass(frozen=True)
class Completion:
    text: str
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class Completer(Protocol):
    def complete(self, system: str, user: str) -> Completion: ...


@dataclass(frozen=True)
class Inference:
    interpretation: Interpretation
    attempts: int
    raw: str
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)


class OpenAiCompatible:
    """Thin wrapper over the OpenAI SDK, which is why the base URL decides the model."""

    def __init__(self, config: LlmConfig) -> None:
        from openai import OpenAI  # imported lazily: tests never need the SDK

        self._client = OpenAI(
            base_url=config.base_url, api_key=config.api_key, timeout=config.timeout
        )
        self._config = config

    def complete(self, system: str, user: str) -> Completion:
        started = time.monotonic()
        response = self._client.chat.completions.create(
            model=self._config.model,
            temperature=self._config.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        elapsed = int((time.monotonic() - started) * 1000)
        message = response.choices[0].message.content or ""
        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        return Completion(text=message, latency_ms=elapsed, usage=usage)


@dataclass(frozen=True)
class CredentialCheck:
    """`invalid` means the provider refused the key; `unverified` means we could not tell."""

    status: Literal["ok", "invalid", "unverified"]
    detail: str = ""


def check_credentials(config: LlmConfig, timeout: float = 15.0) -> CredentialCheck:
    """One-token completion. Network trouble never blocks the user.

    Listing models is not enough: some providers (api.llmapi.ai among them) serve
    `/models` without authentication, so it would accept any key.
    """
    import openai  # imported lazily, like in OpenAiCompatible

    client = openai.OpenAI(
        base_url=config.base_url, api_key=config.api_key, timeout=timeout, max_retries=0
    )
    try:
        client.chat.completions.create(
            model=config.model, max_tokens=1, messages=[{"role": "user", "content": "ok"}]
        )
    except (openai.AuthenticationError, openai.PermissionDeniedError):
        return CredentialCheck("invalid", "el proveedor rechazó la clave")
    except openai.NotFoundError:
        return CredentialCheck(
            "unverified", f"el modelo {config.model!r} no existe en el proveedor"
        )
    except openai.APIConnectionError as exc:
        return CredentialCheck("unverified", f"no se pudo conectar: {exc}")
    except openai.APIStatusError as exc:
        return CredentialCheck("unverified", f"el proveedor respondió HTTP {exc.status_code}")
    return CredentialCheck("ok")


def config_from_env() -> LlmConfig:
    base_url = os.environ.get("GIRA_ISO_LLM_BASE_URL")
    api_key = os.environ.get("GIRA_ISO_LLM_API_KEY")
    if not base_url or not api_key:
        raise InterpretError(
            "set GIRA_ISO_LLM_BASE_URL and GIRA_ISO_LLM_API_KEY "
            "(optionally GIRA_ISO_LLM_MODEL) to call a model"
        )
    return LlmConfig(
        base_url=base_url,
        api_key=api_key,
        model=os.environ.get("GIRA_ISO_LLM_MODEL", DEFAULT_MODEL),
    )


def _strip_fences(text: str) -> str:
    """Some endpoints wrap JSON in fences even in JSON mode."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        stripped = "\n".join(body).strip()
    return stripped


def _parse(text: str) -> Interpretation:
    try:
        payload = json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not JSON: {exc}") from exc
    return Interpretation.model_validate(payload)


def interpret(
    request: str,
    *,
    completer: Completer,
    retries: int = 1,
    context: str | None = None,
) -> Inference:
    """Interpret one request. A validation failure is fed back to the model once."""
    user = user_message(request, context)
    last_error = ""
    raw = ""
    started = time.monotonic()

    for attempt in range(1, retries + 2):
        prompt = user
        if last_error:
            prompt = (
                f"{user}\n\n"
                "Your previous answer was rejected by the schema: "
                f"{last_error}\n"
                "Reply again with a single valid JSON object."
            )
        completion = completer.complete(SYSTEM_PROMPT, prompt)
        raw = completion.text
        try:
            interpretation = _parse(completion.text)
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            continue
        return Inference(
            interpretation=interpretation,
            attempts=attempt,
            raw=raw,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=completion.usage,
        )

    raise InterpretError(
        f"model output failed validation after {retries + 1} attempts: {last_error}"
    )

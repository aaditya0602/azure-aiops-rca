"""Pluggable LLM providers.

Three of them, all speaking the OpenAI chat-completions shape:

  zai            GLM via z.ai. Default for development: cheap and fast.
  azure_foundry  Azure AI Foundry / Azure OpenAI deployment.
  cassette       Replay of previously RECORDED responses, keyed by prompt hash.
                 Makes the eval deterministic and lets CI run with no key and no
                 network. Cassettes are recorded from a real provider with
                 `--record`; they are never hand-written, because a hand-written
                 "recording" would misrepresent what the model actually said.

Keys come from the environment only. Nothing here reads or writes a key to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


class ProviderError(RuntimeError):
    pass


def prompt_key(model: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(system.encode())
    h.update(b"\x00")
    h.update(user.encode())
    return h.hexdigest()[:32]


class Provider:
    name = "base"

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    """Any endpoint exposing POST /chat/completions in the OpenAI shape."""

    # Reasoning models spend a large part of their budget before emitting any
    # content, so a budget sized for a plain chat model comes back empty with
    # finish_reason=stop and no error. 2400 leaves room for the JSON.
    DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2400"))

    def __init__(self, name: str, base_url: str, api_key: str, model: str,
                 max_tokens: int | None = None, temperature: float = 0.0):
        if not api_key:
            raise ProviderError(
                f"{name}: no API key. Set the provider's key env var, or use "
                f"--provider cassette to run offline.")
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens or self.DEFAULT_MAX_TOKENS
        self.temperature = temperature

    def complete(self, system: str, user: str) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Deterministic by default: this output feeds a scored pipeline, and a
            # ranking that changes run to run cannot be regression-tested.
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=60.0,
        )
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {r.status_code}: {r.text[:400]}")
        body = r.json()
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"{self.name}: unexpected response shape: {e}") from e

        if not (content or "").strip():
            # A reasoning model that exhausted its budget returns HTTP 200 with an
            # empty string and finish_reason=length. Silently treating that as a
            # valid answer would look like a model that refuses to answer.
            raise ProviderError(
                f"{self.name}: empty content (finish_reason="
                f"{choice.get('finish_reason')}, "
                f"tokens={body.get('usage', {}).get('total_tokens')}). "
                f"Raise LLM_MAX_TOKENS above {self.max_tokens}.")
        return content


class CassetteProvider(Provider):
    name = "cassette"

    def __init__(self, model: str, cassette_dir: Path = CASSETTE_DIR,
                 record_with: Provider | None = None):
        self.model = model
        self.dir = Path(cassette_dir)
        self.record_with = record_with

    def complete(self, system: str, user: str) -> str:
        key = prompt_key(self.model, system, user)
        path = self.dir / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]

        if self.record_with is None:
            raise ProviderError(
                f"no cassette for prompt {key} in {self.dir}. Record one with "
                f"--record and a real provider, or check that the prompt is "
                f"byte-identical to when it was recorded.")

        response = self.record_with.complete(system, user)
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "key": key,
            "model": self.model,
            "recorded_from": self.record_with.name,
            "system": system,
            "user": user,
            "response": response,
        }, indent=2), encoding="utf-8")
        return response


def build(provider: str, model: str | None = None, record: bool = False) -> Provider:
    """Construct a provider by name.

    Env vars:
      ZAI_API_KEY, ZAI_BASE_URL, ZAI_MODEL
      AZURE_AI_API_KEY, AZURE_AI_BASE_URL, AZURE_AI_MODEL
    """
    provider = provider.lower()

    if provider == "zai":
        return OpenAICompatProvider(
            "zai",
            os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"),
            os.getenv("ZAI_API_KEY", ""),
            model or os.getenv("ZAI_MODEL", "glm-4.6"),
        )

    if provider in ("azure_foundry", "azure"):
        base = os.getenv("AZURE_AI_BASE_URL", "")
        if not base:
            raise ProviderError(
                "azure_foundry: set AZURE_AI_BASE_URL to your Foundry/Azure "
                "OpenAI endpoint (…/openai/v1 or the models route).")
        return OpenAICompatProvider(
            "azure_foundry", base, os.getenv("AZURE_AI_API_KEY", ""),
            model or os.getenv("AZURE_AI_MODEL", "gpt-4o-mini"),
        )

    if provider == "cassette":
        inner = None
        if record:
            # Record through whichever real provider is configured.
            inner = build(os.getenv("RECORD_PROVIDER", "zai"), model)
        return CassetteProvider(model or os.getenv("ZAI_MODEL", "glm-4.6"),
                                record_with=inner)

    raise ProviderError(f"unknown provider {provider!r}")

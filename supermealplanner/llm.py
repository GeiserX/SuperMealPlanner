"""Talk to whatever language model the household chose. Optional: with provider "none" the bot only
answers its commands.

Providers (settings.yaml, `llm`):
- openai: any OpenAI-compatible chat endpoint. Set base_url (OpenAI, OpenRouter, Ollama's /v1,
  LM Studio, vLLM...) and model; the key comes from the env var named in api_key_env (may be empty
  for a local model).
- anthropic: the Anthropic Messages API; model and key as above.
- plugin: a Python file under the data directory that defines
      def complete(system: str, messages: list[dict], settings: dict) -> str
  (or an async def). Use it for anything else: a local CLI, an internal gateway, a mock.
Messages are [{"role": "user"|"assistant", "content": str}, ...].
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os

import httpx

from .model import Data


class LLMError(Exception):
    pass


async def complete(data: Data, system: str, messages: list[dict]) -> str:
    cfg = data.settings.llm
    key = os.environ.get(cfg.api_key_env, "")
    if cfg.provider == "none":
        raise LLMError("no provider configured")
    if cfg.provider == "openai":
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{base}/chat/completions", json=body, headers=headers)
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"].strip()
    if cfg.provider == "anthropic":
        base = (cfg.base_url or "https://api.anthropic.com").rstrip("/")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "messages": messages,
        }
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{base}/v1/messages", json=body, headers=headers)
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    if cfg.provider == "plugin":
        fn = _plugin(data)
        if inspect.iscoroutinefunction(fn):
            out = await fn(system, messages, cfg.model_dump())
        else:  # a blocking plugin runs off the event loop
            out = await asyncio.to_thread(fn, system, messages, cfg.model_dump())
        return str(out).strip()
    raise LLMError(f"unknown provider {cfg.provider}")


async def transcribe(data: Data, audio: bytes, filename: str) -> str:
    """Speech to text for voice notes, through the configured transcription provider."""
    cfg = data.settings.transcription
    if cfg.provider == "none":
        raise LLMError("no transcription provider configured")
    if cfg.provider == "openai":
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        key = os.environ.get(cfg.api_key_env, "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        form = {"model": cfg.model}
        if cfg.language:
            form["language"] = cfg.language
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(
                f"{base}/audio/transcriptions",
                data=form,
                files={"file": (filename, audio)},
                headers=headers,
            )
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")
        return r.json().get("text", "").strip()
    if cfg.provider == "plugin":
        fn = _load(data, cfg.plugin, "transcribe")
        if inspect.iscoroutinefunction(fn):
            out = await fn(audio, filename, cfg.model_dump())
        else:
            out = await asyncio.to_thread(fn, audio, filename, cfg.model_dump())
        return str(out).strip()
    raise LLMError(f"unknown transcription provider {cfg.provider}")


def _plugin(data: Data):
    return _load(data, data.settings.llm.plugin, "complete")


def _load(data: Data, rel: str | None, name: str):
    if not rel:
        raise LLMError("a plugin provider needs a .py file under the data directory")
    path = (data.root / rel).resolve()
    if data.root.resolve() not in path.parents:
        raise LLMError("a plugin must live inside the data directory")
    spec = importlib.util.spec_from_file_location(f"smp_plugin_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, name):
        raise LLMError(f"{rel} defines no {name}()")
    return getattr(mod, name)

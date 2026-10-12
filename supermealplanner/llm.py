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

A plugin that keeps a conversation on its side (one ongoing session per chat, so each message sends
only what is new) defines instead, or as well:
      def converse(turn: dict, settings: dict) -> str | dict
      def forget(conversation: str, settings: dict) -> None      # optional, on /new
turn is {"conversation": the id it returned last time in this chat, or None,
         "rules": the stable instructions, "context": the household's data as of now,
         "new": the messages it has not seen yet (every recent message when conversation is None),
         "history": the earlier messages, a recap for when it has to start over,
         "files": {"recipes/<id>.yaml": text, "ingredients.yaml": text}}.
It returns the answer, or {"text": answer, "conversation": id or None, "kept": bool}. The app keeps
the id per chat and sends it back with the next message; None starts over with the full history next
time, and kept False (the provider did not store this turn) sends these messages again.

The app sets turn["proposals"] = True when it can carry out proposals. Then the dict may also hold
"proposals": [{"id": str, "summary": str, "confirm_by": "asker" | "admins" | "none", ...}, ...], things
the plugin would do outside the chat (open a pull request, for example). The app shows each summary as
plain text and, after a yes from the person who asked ("asker") or from one of bot.admins ("admins"),
calls
      def act(proposal: dict, settings: dict) -> str | dict
with the same dict. "none" (a step that changes nothing, such as drafting) runs at once, in the
background, and only for a household member. act returns the text to post, or {"text": ...,
"proposals": [...]} with follow-ups, which are offered the same way.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx

from .model import Data


class LLMError(Exception):
    pass


@dataclass
class Reply:
    text: str
    conversation: str | None = None
    kept: bool = True
    proposals: list[dict] = field(default_factory=list)


CONFIRM_BY = ("asker", "admins", "none")


def _proposals(raw) -> list[dict]:
    """The well-formed proposals in a plugin's answer; anything else is dropped."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if (
            isinstance(p, dict)
            and isinstance(p.get("id"), str)
            and p["id"]
            and isinstance(p.get("summary"), str)
            and p["summary"].strip()
            and p.get("confirm_by") in CONFIRM_BY
        ):
            out.append(p)
    return out


async def converse(data: Data, turn: dict) -> Reply:
    """One answer in a chat. A plugin with converse() keeps the conversation; every other provider
    gets the rules, the context and the recent messages, as complete() always did."""
    cfg = data.settings.llm
    fn = _optional(data, "converse") if cfg.provider == "plugin" else None
    if fn is None:
        messages = _merge([*turn["history"], *turn["new"]])
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(conversation start)"})
        system = f"{turn['rules']}\n\n{turn['context']}"
        return Reply(await complete(data, system, messages))
    out = await _call(fn, {**turn, "proposals": True}, cfg.model_dump())
    if isinstance(out, dict):
        conv = out.get("conversation")
        return Reply(
            str(out.get("text", "")).strip(),
            str(conv) if conv else None,
            bool(out.get("kept", True)),
            _proposals(out.get("proposals")),
        )
    return Reply(str(out).strip())


async def act(data: Data, proposal: dict) -> Reply:
    """Carry out a proposal the plugin made, once the right person said yes."""
    fn = _optional(data, "act") if data.settings.llm.provider == "plugin" else None
    if fn is None:
        raise LLMError("the provider cannot carry out proposals")
    out = await _call(fn, proposal, data.settings.llm.model_dump())
    if isinstance(out, dict):
        return Reply(str(out.get("text", "")).strip(), proposals=_proposals(out.get("proposals")))
    return Reply(str(out).strip())


async def forget(data: Data, conversation: str) -> None:
    """Tell a plugin that keeps conversations to drop one (best effort; /new already started over)."""
    fn = _optional(data, "forget") if data.settings.llm.provider == "plugin" else None
    if fn is not None:
        await _call(fn, conversation, data.settings.llm.model_dump())


async def _call(fn, *args):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)  # a blocking plugin runs off the event loop


def _merge(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {**out[-1], "content": out[-1]["content"] + "\n" + m["content"]}
        else:
            out.append(dict(m))
    return out


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


def _optional(data: Data, name: str):
    try:
        return _load(data, data.settings.llm.plugin, name)
    except LLMError as e:
        if "defines no" in str(e):
            return None
        raise


@lru_cache(maxsize=8)
def _module(path: str, mtime_ns: int):
    """One module per plugin file version: state a plugin keeps survives between calls, and an
    edited file (a data repo pull) is loaded again."""
    spec = importlib.util.spec_from_file_location(f"smp_plugin_{Path(path).stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load(data: Data, rel: str | None, name: str):
    if not rel:
        raise LLMError("a plugin provider needs a .py file under the data directory")
    path = (data.root / rel).resolve()
    if data.root.resolve() not in path.parents:
        raise LLMError("a plugin must live inside the data directory")
    try:
        mod = _module(str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        raise LLMError(f"{rel} does not exist") from None
    if not hasattr(mod, name):
        raise LLMError(f"{rel} defines no {name}()")
    return getattr(mod, name)

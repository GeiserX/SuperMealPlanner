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
         "files": {"recipes/<id>.yaml": text, "ingredients.yaml": text},
         "attachments": the photos and files sent with this turn's messages, each
             {"name": "inbox/<message id>-<file name>", "mime": the type Telegram gives,
              "data": bytes, "from": person id or None}; any kind of file;
             the message's text names the file as "[photo: <name>]", "[video: <name>]",
             "[animation: <name>]", "[video note: <name>]", "[sticker: <name>]" or
             "[file: <name>]",
         "chat": {"id": the Telegram chat id, "user": the Telegram user id of the person whose
             message started this turn, or None}; a provider that can read the chat's earlier
             messages elsewhere (an archive of the chat) uses it to look up only this chat}.
Only this provider gets photos and files (llm.sees_files): the bot tells the person any other one
cannot look at them. A photo or a file that arrives while an answer is being written waits for the
next turn, which carries it.
A provider that is out of capacity raises an error with a `retry_after` attribute (seconds): the
app tells the chat how long the wait is, once, and answers the waiting messages by itself then.
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

Messages that arrive while an answer is being written join it, so a burst gets one answer. Every
message in turn["new"] and turn["history"] carries "ids" (the app's ids of the messages merged into
it) and "ts" (when the last of them was written, ISO 8601 UTC). The app also sets
      turn["incoming"]() -> list of {"id": int, "role": "user", "content": str, "ts": str}
which a plugin may call from its thread at any time while it works on the answer: each call hands
over the messages that arrived since the last call (an edit of a message of this turn arrives as a
correction in the same shape). The dict it returns then names the ones its answer took into
account, "covers": [ids]; every handed-over message it does not name is sent again with the next
turn. A plugin that never calls incoming() covers nothing extra: those messages make the next turn.

The answer may offer the person choices: "options": ["short label", ...] in the dict (at most five),
or an ```options block at the end of the text with one "- label" per line. The app shows them as
buttons under the answer; a tap is that person's next message. They never wait for a tap.

The answer may offer reminders: when it tells someone to do something at a time (cook on Sunday
afternoon, take the fish out of the freezer on Saturday night), "reminders": [{"when": "YYYY-MM-DD
HH:MM", "text": str, "person": person id or None}, ...] in the dict (at most five, the time in the
household's timezone), or a ```reminders block at the end of the text with one
"- YYYY-MM-DD HH:MM | text" line each (" | <person id>" at the end names whom it is for). The app
posts them in one message with buttons per reminder: a reminder here (the app posts the text in the
chat at that time), in the calendar (with bot.reminder_calendar: a new turn asks the provider to add
the event, and that tap approves its first ask of that turn when the ask adds an event at that very
time), or nothing. An offer nobody taps expires and nothing happens. A reminder with "set": true (a
fourth "| set" field in the block) is one the person asked for: the app sets it at once and says
so, with no offer. {"cancel": id} (a "- cancel | <id>" line) cancels a reminder set from that chat.

A plugin whose actions need a person's yes while it works defines
      def answer(request: dict, decision: dict, settings: dict) -> dict
and gets turn["ask"](request) from the app, callable from its thread: request is {"id": str,
"summary": str, "confirm_by": "asker" | "admins", "expires_at": epoch seconds}. The app posts the
summary with Approve and Reject buttons (a request may also say "action": "add" | "change" |
"delete" and "when": the event's start in epoch seconds, so a tap on a reminder's "in the calendar"
can approve the event it asked for), and on a tap by the right person (or at once, with
approved False and a note, when nobody can be asked, or when that person edits a message of the
turn) calls answer(request, {"approved": bool, "by": str, "note": str | None}, settings), which
returns {"ok": bool | None, "error": str | None}: the app then says done or failed under the ask.
Past expires_at, or once the turn ends, the buttons go.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx

from .model import Data


class LLMError(Exception):
    pass


class NoAct(LLMError):
    """The provider has no act(), so it cannot carry out proposals."""


@dataclass
class Reply:
    text: str
    conversation: str | None = None
    kept: bool = True
    proposals: list[dict] = field(default_factory=list)
    covers: set[int] = field(default_factory=set)  # handed-over messages the answer took in
    options: list[str] = field(default_factory=list)  # choices to offer as buttons
    reminders: list[dict] = field(default_factory=list)  # {"when", "text", "person"} to offer


CONFIRM_BY = ("asker", "admins", "none")
OPTIONS_MAX, OPTION_CHARS = 5, 40
OPTIONS = re.compile(r"\n?```options[ \t]*\n(.*?)```[ \t]*", re.DOTALL)


def options(raw) -> list[str]:
    """Well-formed choices: short, distinct, at most OPTIONS_MAX; anything else is dropped."""
    out: list[str] = []
    for o in raw if isinstance(raw, list) else []:
        label = " ".join(str(o).split()) if isinstance(o, str | int | float) else ""
        if label and len(label) <= OPTION_CHARS and label not in out:
            out.append(label)
    return out[:OPTIONS_MAX]


def extract_options(text: str) -> tuple[str, list[str]]:
    """The answer without its ```options block, and the choices it lists."""
    blocks = list(OPTIONS.finditer(text))
    if not blocks:
        return text, []
    lines = [x.strip() for x in blocks[-1].group(1).splitlines()]
    labels = [x[1:].strip() for x in lines if x.startswith(("-", "*"))]
    return OPTIONS.sub("", text).strip(), options(labels)


REMINDERS_MAX, REMINDER_CHARS = 5, 300
REMINDERS = re.compile(r"\n?```reminders[ \t]*\n(.*?)```[ \t]*", re.DOTALL)
WHEN = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


def reminders(raw) -> list[dict]:
    """Well-formed reminders: a time written as YYYY-MM-DD HH:MM, a short text, a person id or None,
    and "set" when the person asked for it; or {"cancel": id} for a reminder set from the chat that
    is no longer right. At most REMINDERS_MAX of each, anything else is dropped. Whether the time is still to come is the
    app's to judge when it offers them."""
    out: list[dict] = []
    cancels: list[dict] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        gone = str(r.get("cancel") if r.get("cancel") is not None else "").strip()
        if gone.isdigit():  # a reminder set from the chat that the answer made wrong
            if {"cancel": int(gone)} not in cancels:
                cancels.append({"cancel": int(gone)})
            continue
        when = str(r.get("when") or "").strip()
        text = " ".join(str(r.get("text") or "").split())
        person = r.get("person")
        if not WHEN.fullmatch(when) or not text or len(text) > REMINDER_CHARS:
            continue
        item = {
            "when": when.replace("T", " "),
            "text": text,
            "person": str(person) if isinstance(person, str) and person.strip() else None,
        }
        if r.get("set") is True:  # asked for in so many words: set without an offer
            item["set"] = True
        if all((o.get("when"), o.get("text")) != (item["when"], item["text"]) for o in out):
            out.append(item)
    return out[:REMINDERS_MAX] + cancels[:REMINDERS_MAX]


def extract_reminders(text: str) -> tuple[str, list[dict]]:
    """The answer without its ```reminders block, and the reminders it lists."""
    blocks = list(REMINDERS.finditer(text))
    if not blocks:
        return text, []
    found = []
    for line in blocks[-1].group(1).splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        parts = [x.strip() for x in line[1:].split("|")]
        if len(parts) >= 2 and parts[0].lower() == "cancel":
            found.append({"cancel": parts[1]})
        elif len(parts) >= 2:
            found.append(
                {
                    "when": parts[0],
                    "text": parts[1],
                    "person": (parts[2] or None) if len(parts) > 2 else None,
                    "set": len(parts) > 3 and parts[3].lower() == "set",
                }
            )
    return REMINDERS.sub("", text).strip(), reminders(found)


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
        covers = out.get("covers")
        return Reply(
            str(out.get("text", "")).strip(),
            str(conv) if conv else None,
            bool(out.get("kept", True)),
            _proposals(out.get("proposals")),
            {int(i) for i in covers if isinstance(i, int)} if isinstance(covers, list) else set(),
            options(out.get("options")),
            reminders(out.get("reminders")),
        )
    return Reply(str(out).strip())


async def answer(data: Data, request: dict, decision: dict) -> dict:
    """Hand a person's answer to an ask back to the plugin; {"ok", "error"} from its answer()."""
    fn = _optional(data, "answer") if data.settings.llm.provider == "plugin" else None
    if fn is None:
        raise NoAct("the provider asks for nothing")
    out = await _call(fn, request, decision, data.settings.llm.model_dump())
    out = out if isinstance(out, dict) else {}
    ok = out.get("ok")
    return {"ok": ok if isinstance(ok, bool) else None, "error": out.get("error")}


def sees_files(data: Data) -> bool:
    """The provider gets the photos and files people send (a plugin with converse())."""
    return data.settings.llm.provider == "plugin" and _optional(data, "converse") is not None


def asks(data: Data) -> bool:
    """The provider may ask a person for a yes while it works (it defines answer())."""
    return data.settings.llm.provider == "plugin" and _optional(data, "answer") is not None


async def act(data: Data, proposal: dict) -> Reply:
    """Carry out a proposal the plugin made, once the right person said yes."""
    fn = _optional(data, "act") if data.settings.llm.provider == "plugin" else None
    if fn is None:
        raise NoAct("the provider cannot carry out proposals")
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
        else:  # only what a chat API takes: the app's ids and times stay here
            out.append({"role": m["role"], "content": m["content"]})
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

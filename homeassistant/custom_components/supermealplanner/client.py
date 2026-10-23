"""The SuperMealPlanner API calls this integration makes: pair, say, wait for the answer, transcribe."""

from __future__ import annotations

import time

import aiohttp

WAIT = 25  # what one GET /chef/{id} holds at most on the server
TIMEOUT = aiohttp.ClientTimeout(total=WAIT + 15, connect=10)


class ChefError(Exception):
    """The server could not be reached, or refused."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class Slow(Exception):
    """The chef had no answer within the time we wait."""


async def _json(resp: aiohttp.ClientResponse) -> dict:
    if resp.status >= 400:
        raise ChefError(resp.status, (await resp.text())[:200])
    return await resp.json()


async def pair(session: aiohttp.ClientSession, url: str, code: str, device: str) -> dict:
    """Swap a six-digit code from the bot's /pair for a device token."""
    try:
        async with session.post(
            f"{url}/api/v1/pair", json={"code": code, "device": device}, timeout=TIMEOUT
        ) as resp:
            return await _json(resp)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ChefError(None, str(err)) from err


class Chef:
    """One paired device: everything said through it is said by that device's person."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self.session = session
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def ask(self, text: str, budget: float) -> str:
        """Say `text` to the chef and return its answer, waiting `budget` seconds at most."""
        deadline = time.monotonic() + budget
        try:
            async with self.session.post(
                f"{self.url}/api/v1/chef",
                json={"text": text},
                headers=self.headers,
                timeout=TIMEOUT,
            ) as resp:
                turn = await _json(resp)
            while turn.get("state") == "thinking":
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Slow
                async with self.session.get(
                    f"{self.url}/api/v1/chef/{turn['id']}",
                    params={"wait": str(int(min(WAIT, max(1, left))))},
                    headers=self.headers,
                    timeout=TIMEOUT,
                ) as resp:
                    turn = await _json(resp)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ChefError(None, str(err)) from err
        return str(turn.get("text") or "")

    async def transcribe(self, wav: bytes) -> str:
        """Speech to text with the server's own transcriber."""
        try:
            async with self.session.post(
                f"{self.url}/api/v1/transcribe",
                data=wav,
                headers={**self.headers, "Content-Type": "audio/wav"},
                timeout=aiohttp.ClientTimeout(total=120, connect=10),
            ) as resp:
                return str((await _json(resp)).get("text") or "")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ChefError(None, str(err)) from err

    async def ping(self) -> None:
        """Raise unless the server answers; checked before the entry is set up."""
        try:
            async with self.session.get(f"{self.url}/api/v1/ping", timeout=TIMEOUT) as resp:
                await _json(resp)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ChefError(None, str(err)) from err

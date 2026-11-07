"""Two model backends with the same models: 💳 OpenRouter (paid) and 🆓 NVIDIA NIM (free), switched by hand.

- ModelRouter looks like an OpenAI client (client.chat.completions.create) and sends every request to the backend
  the owner picked in ⚙️ Settings, so chat, the coding lead and the agents all follow the switch.
- KeyPool spreads NVIDIA requests over several API keys instead of hammering one:
  * each request goes to the key with the fewest requests in the last minute (ties: the one used longest ago),
  * a key never goes over its per-minute limit (rpm_per_key) — if every key is full, the request waits for the
    first free slot instead of being rejected,
  * a 429 puts that key on a short cooldown and the request moves to another key at once,
  * a 401/403 takes the key out for a while (wrong or revoked), a 5xx / dropped connection is a brief cooldown.
- ping() sends a 1-token request through one key to check that it really answers.
"""

import asyncio
import copy
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

log = logging.getLogger("persona-bot.backends")

WINDOW = 60.0           # seconds: the per-key rate window
COOLDOWN_429 = 20.0     # seconds a rate-limited key rests when the server gives no Retry-After
COOLDOWN_ERROR = 5.0    # seconds after a 5xx or a dropped connection
DEAD_AUTH = 900.0       # seconds a rejected key (401/403) stays out


class NoKeyAvailable(RuntimeError):
    """Every NVIDIA key is missing, rejected or still busy after waiting."""


@dataclass
class Slot:
    name: str                      # "key 1", "key 2"… — never the key itself
    client: object
    recent: deque = field(default_factory=deque)   # start times of recent requests
    cooldown_until: float = 0.0
    dead_until: float = 0.0
    total: int = 0
    failures: int = 0
    last_status: str = "unused"
    last_used: float = 0.0

    def load(self, now: float) -> int:
        while self.recent and now - self.recent[0] >= WINDOW:
            self.recent.popleft()
        return len(self.recent)


def _retry_after(e: APIStatusError) -> float | None:
    try:
        value = e.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


class KeyPool:
    def __init__(self, keys: list[str], base_url: str, rpm_per_key: int = 40, max_wait: float = 45.0,
                 client_factory=None, clock=time.monotonic):
        factory = client_factory or (lambda key: AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0))
        self.slots = [Slot(f"key {i + 1}", factory(k)) for i, k in enumerate(dict.fromkeys(k for k in keys if k))]
        self.rpm = max(1, int(rpm_per_key))
        self.max_wait = max_wait
        self.clock = clock
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self.slots)

    def _usable(self, s: Slot, now: float) -> bool:
        return now >= s.dead_until and now >= s.cooldown_until and s.load(now) < self.rpm

    def pick(self) -> Slot | None:
        now = self.clock()
        usable = [s for s in self.slots if self._usable(s, now)]
        if not usable:
            return None
        return min(usable, key=lambda s: (s.load(now), s.last_used))

    def _next_free_in(self) -> float | None:
        """Seconds until some key can take a request again (None: every key is rejected)."""
        now = self.clock()
        waits = []
        for s in self.slots:
            if now < s.dead_until:
                continue
            w = max(0.0, s.cooldown_until - now)
            if s.load(now) >= self.rpm:
                w = max(w, WINDOW - (now - s.recent[0]))
            waits.append(w)
        return min(waits) if waits else None

    async def _take(self) -> Slot:
        deadline = self.clock() + self.max_wait
        while True:
            async with self._lock:
                slot = self.pick()
                if slot:
                    now = self.clock()
                    slot.recent.append(now)
                    slot.last_used = now
                    slot.total += 1
                    return slot
                wait = self._next_free_in()
            if not self.slots:
                raise NoKeyAvailable("No NVIDIA API keys are set (add the NVIDIA_KEY_1..3 secrets).")
            if wait is None:
                raise NoKeyAvailable("Every NVIDIA key was rejected (401/403) — check or replace the keys.")
            if self.clock() + wait > deadline:
                raise NoKeyAvailable("All NVIDIA keys are at their rate limit right now — try again in a minute, "
                                     "or switch to 💳 OpenRouter in ⚙️ Settings.")
            await asyncio.sleep(min(max(wait, 0.05), 5.0))

    async def create(self, **kw):
        """One request through the pool, moving to another key on 429 / auth / server errors."""
        last: Exception | None = None
        for _ in range(max(2, 2 * len(self.slots))):
            slot = await self._take()
            try:
                res = await slot.client.chat.completions.create(**kw)
                slot.last_status = "ok"
                return res
            except APIStatusError as e:
                last = e
                slot.failures += 1
                code = e.status_code
                slot.last_status = f"HTTP {code}"
                if code == 429:
                    slot.cooldown_until = self.clock() + (_retry_after(e) or COOLDOWN_429)
                elif code in (401, 403):
                    slot.dead_until = self.clock() + DEAD_AUTH
                elif code >= 500:
                    slot.cooldown_until = self.clock() + COOLDOWN_ERROR
                else:
                    raise  # a bad request fails the same on every key
                log.warning("NVIDIA %s answered %s — trying another key", slot.name, code)
            except (APIConnectionError, APITimeoutError) as e:
                last = e
                slot.failures += 1
                slot.last_status = "connection error"
                slot.cooldown_until = self.clock() + COOLDOWN_ERROR
        raise last or NoKeyAvailable("No NVIDIA key could take the request")

    async def ping(self, slot: Slot, model: str, timeout: float = 30.0) -> tuple[bool | None, str, float]:
        """Checks one key in two steps, so a slow model is never mistaken for a bad key:
        1) the request is accepted (a wrong key is refused within a second: 401/403),
        2) the first streamed token arrives (the free service may queue requests for a while).
        Returns (True = works / None = key accepted but the model is slow / False = broken, status, seconds)."""
        start = self.clock()
        slot.recent.append(start)
        slot.total += 1
        stream = None
        try:
            stream = await asyncio.wait_for(slot.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=8, stream=True), timeout)
            accepted = self.clock() - start
            try:
                await asyncio.wait_for(stream.__anext__(), max(5.0, timeout * 2 - accepted))
            except StopAsyncIteration:
                pass
            except asyncio.TimeoutError:
                slot.last_status = "slow"
                return None, (f"key accepted ({accepted:.1f}s) · the model is busy — no token yet, "
                              f"requests may wait in NVIDIA's queue"), self.clock() - start
            slot.last_status = "ok"
            return True, f"answers · first token in {self.clock() - start:.1f}s", self.clock() - start
        except APIStatusError as e:
            slot.last_status = f"HTTP {e.status_code}"
            meaning = {401: "key rejected (401)", 403: "key rejected or no access (403)",
                       429: "rate limited right now (429)",
                       404: "model not found (404)"}.get(e.status_code, f"HTTP {e.status_code}")
            if e.status_code == 429:
                slot.cooldown_until = self.clock() + (_retry_after(e) or COOLDOWN_429)
            elif e.status_code in (401, 403):
                slot.dead_until = self.clock() + DEAD_AUTH
            return False, meaning, self.clock() - start
        except asyncio.TimeoutError:
            slot.last_status = "timeout"
            return False, f"NVIDIA didn't even accept the request in {timeout:.0f}s (network or overload)", \
                self.clock() - start
        except Exception as e:  # connection errors and the like
            slot.last_status = type(e).__name__
            return False, f"{type(e).__name__}", self.clock() - start
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    await stream.close()
                except Exception:
                    pass

    def status(self) -> list[dict]:
        now = self.clock()
        out = []
        for s in self.slots:
            state = ("rejected" if now < s.dead_until else
                     f"cooling {s.cooldown_until - now:.0f}s" if now < s.cooldown_until else "ready")
            out.append({"name": s.name, "state": state, "last_minute": s.load(now), "total": s.total,
                        "failures": s.failures, "last": s.last_status})
        return out


def to_nvidia(kw: dict, models: dict, model_extra: dict) -> dict:
    """An OpenRouter-shaped request → the NVIDIA one: the NIM model id, no OpenRouter-only fields."""
    out = {k: v for k, v in kw.items() if k not in ("extra_headers", "extra_body")}
    model = kw.get("model")
    out["model"] = models.get(model, model)
    extra = model_extra.get(model) or model_extra.get(out["model"])
    if extra:
        out["extra_body"] = copy.deepcopy(extra)
    msgs = []
    for m in kw.get("messages") or []:
        if isinstance(m, dict) and "reasoning_details" in m:
            m = {k: v for k, v in m.items() if k != "reasoning_details"}
        msgs.append(m)
    out["messages"] = msgs
    return out


class ModelRouter:
    """Looks like an OpenAI client; each request goes to the backend picked right now."""

    def __init__(self, openrouter, pool: KeyPool | None, backend_fn, models: dict, model_extra: dict | None = None):
        self.openrouter, self.pool, self.backend_fn = openrouter, pool, backend_fn
        self.models, self.model_extra = models, model_extra or {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def backend(self) -> str:
        return "nvidia" if self.backend_fn() == "nvidia" else "openrouter"

    async def _create(self, **kw):
        if self.backend() == "nvidia":
            if not self.pool or not len(self.pool):
                raise NoKeyAvailable("🆓 NVIDIA is selected but no NVIDIA keys are set — add the NVIDIA_KEY_1..3 "
                                     "secrets, or switch back to 💳 OpenRouter in ⚙️ Settings.")
            return await self.pool.create(**to_nvidia(kw, self.models, self.model_extra))
        return await self.openrouter.chat.completions.create(**kw)

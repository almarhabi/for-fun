"""Where the models run: OpenRouter (paid) and any number of free providers (NVIDIA, AIHubMix, OrcaRouter…),
with a smart router in front of them.

Pieces
- KeyPool: one provider's keys (any number, one per line in one secret). Each request goes to the key with the
  fewest requests in the last minute (then the fewest today, then the earlier line). No key ever goes over its
  per-minute or per-day limit; a 429 rests that key and the request moves to another key at once; a rejected key
  (401/402/403) is taken out; a provider's "daily quota" 429 rests the key for an hour. Daily counts survive
  restarts (usage_store), so a 100-requests-a-day key isn't overrun after the bot restarts.
- Provider: a model source — its keys or client, which models it serves (under which ids), and extra request
  fields (e.g. how to turn thinking on).
- ModelRouter: looks like an OpenAI client. Each role (main = chat + the coding lead, agents = the coding agents)
  has its own router and its own choice: one provider pinned, or "smart".

Smart routing (free first)
- Every (provider, model) pair keeps live health: time to first token (moving average), a failure rate, a failure
  streak with a circuit breaker (30 s → 60 s → … → 10 min out), and when it was last seen. Old observations fade,
  so a provider that was slow an hour ago gets another chance.
- Candidates are ranked by expected wait: time to first token × failure penalty × load, plus the time until a key
  frees up, with a penalty when a daily quota runs low (save it for later) and a bonus for the provider that served
  the same conversation last time (provider-side caching, steady behaviour).
- Hedging: the best candidate starts; if it hasn't produced a token after ~1.5× its usual wait (10–45 s), the next
  one starts too — whichever answers first wins and the others are cancelled (their slowness is remembered). An
  error moves on to the next candidate at once. Requests are plain completions, so racing them is safe.
- Paid as last resort (optional): OpenRouter joins at the end of the line, only after every free option failed.
- Pinned providers get a bounded wait for the first token and one retry, then a clear message — never a silent
  half-hour hang. A stream that dies midway counts against its provider.
"""

import asyncio
import contextvars
import copy
import hashlib
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

log = logging.getLogger("persona-bot.backends")

WINDOW = 60.0           # seconds: the per-key rate window
DAY = 86_400.0          # seconds: the per-key daily window (rolling)
COOLDOWN_429 = 20.0     # seconds a rate-limited key rests when the server gives no Retry-After
COOLDOWN_QUOTA = 3600.0  # seconds a key rests after a "daily quota used up" 429
COOLDOWN_ERROR = 5.0    # seconds after a 5xx or a dropped connection
DEAD_AUTH = 900.0       # seconds a rejected key (401/402/403) stays out
PRIOR_TTFT = 8.0        # seconds: what we assume about a provider we haven't measured yet

# Set by callers so the router can keep a conversation on the provider that served it last time
ROUTE_KEY: contextvars.ContextVar = contextvars.ContextVar("route_key", default="")
# Set by callers to hear what the router is doing ("waiting for NVIDIA", "also trying AIHubMix", "via …")
ROUTE_EVENTS: contextvars.ContextVar = contextvars.ContextVar("route_events", default=None)


class NoKeyAvailable(RuntimeError):
    """A provider has no usable key (missing, rejected, or all at their limits)."""


class ProviderSlow(NoKeyAvailable):
    """The provider accepted the request but never started answering (a free service's queue)."""


NvidiaSlow = ProviderSlow  # older name


class NoRoute(NoKeyAvailable):
    """Nothing can serve this model with the current choice."""


def _emit(text: str) -> None:
    fn = ROUTE_EVENTS.get()
    if fn:
        try:
            fn(text)
        except Exception:
            pass


def parse_keys(*chunks: str) -> list[str]:
    """Keys from secrets: one per line (commas and spaces work too); quotes trimmed; duplicates dropped; order kept."""
    keys = []
    for chunk in chunks:
        for line in (chunk or "").replace(",", "\n").split():
            k = line.strip().strip("\"'")
            if k.lower() == "bearer":   # "Bearer sk-…" pasted whole: the next word is the key
                continue
            if k:
                keys.append(k)
    return list(dict.fromkeys(keys))


def _meaningful(chunk) -> bool:
    """Does this chunk show the model really started (text, reasoning, a tool call, the end, or usage)?
    Some servers send an empty "role" chunk the moment they accept a request — that doesn't count."""
    if getattr(chunk, "usage", None):
        return True
    for c in getattr(chunk, "choices", None) or []:
        if getattr(c, "finish_reason", None):
            return True
        d = getattr(c, "delta", None)
        if d is None:
            continue
        extra = getattr(d, "model_extra", None) or {}
        if (getattr(d, "content", None) or getattr(d, "tool_calls", None) or getattr(d, "reasoning_content", None)
                or extra.get("reasoning") or extra.get("reasoning_content") or extra.get("reasoning_details")):
            return True
    return not getattr(chunk, "choices", None) and not hasattr(chunk, "usage")  # unknown shape: trust it


async def first_chunks(stream, limit: int = 30) -> list:
    """Reads until the first meaningful chunk; returns everything read (the stream continues after it).
    An empty list = the stream ended at once."""
    got = []
    while True:
        try:
            chunk = await stream.__anext__()
        except StopAsyncIteration:
            return got
        got.append(chunk)
        if _meaningful(chunk) or len(got) >= limit:
            return got


class PrefetchedStream:
    """A stream whose first chunks were already read (to know the model really started), reporting how it ends."""

    def __init__(self, first, rest, on_error=None):
        self._buf = list(first) if isinstance(first, list) else ([] if first is StopAsyncIteration else [first])
        self._ended = first is StopAsyncIteration
        self._rest, self._on_error = rest, on_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._buf:
            return self._buf.pop(0)
        if self._ended:
            raise StopAsyncIteration
        try:
            return await self._rest.__anext__()
        except (StopAsyncIteration, asyncio.CancelledError):
            raise
        except Exception as e:  # died midway: it counts against the provider
            if self._on_error:
                self._on_error(e)
            raise

    async def close(self):
        if hasattr(self._rest, "close"):
            try:
                await self._rest.close()
            except Exception:
                pass


# ================= Patience =================

def retryable(e: BaseException) -> bool:
    """Will trying again (later, or elsewhere) likely work? Timeouts, dropped connections, overload, server errors,
    busy keys: yes. A wrong key, no credit, a bad request, a model nobody serves: no — say so at once."""
    if isinstance(e, asyncio.CancelledError):
        return False
    if getattr(e, "retry", None) is not None:   # set by the router on a combined error
        return bool(e.retry)
    if isinstance(e, (APIConnectionError, APITimeoutError, asyncio.TimeoutError, httpx.TransportError)):
        return True
    if isinstance(e, APIStatusError):
        return e.status_code in (408, 409, 425, 429) or e.status_code >= 500
    if isinstance(e, NoKeyAvailable):
        msg = str(e)
        return not any(w in msg for w in ("keys are set", "was rejected", "doesn't serve", "Nothing serves",
                                           "no free provider with keys", "has no keys", "used up today"))
    if isinstance(e, APIError):  # an error event inside the stream ("provider returned error", "overloaded")
        msg = str(e).lower()
        return not ("tool" in msg and ("support" in msg or "endpoint" in msg))
    return False


def short_reason(e: BaseException) -> str:
    """A few words for the live status: why it's trying again."""
    if isinstance(e, (APITimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "The connection timed out"
    if isinstance(e, (APIConnectionError, httpx.TransportError)):
        return "The connection dropped"
    if isinstance(e, APIStatusError):
        return "The source is busy (429)" if e.status_code == 429 else f"Server error ({e.status_code})"
    if isinstance(e, ProviderSlow):
        return "No source started answering"
    if isinstance(e, NoKeyAvailable):
        return "Every key is at its limit"
    return "The source failed"


async def persist(call, on_wait=None, window: float = 1800.0, clock=time.monotonic, sleep=None):
    """Runs `await call()` until it works. A failure that waiting can fix is retried for up to `window` seconds,
    with growing pauses (3 s, 6 s, 12 s … at most 60 s); smart routing picks the healthiest source each time,
    so a failing one is left behind. on_wait(error, attempt, pause) is told before every pause."""
    start, attempt = clock(), 0
    sleep = sleep or asyncio.sleep
    while True:
        try:
            return await call()
        except Exception as e:
            attempt += 1
            if not retryable(e) or clock() - start >= window:
                raise
            pause = min(60.0, 3.0 * 2 ** (attempt - 1))
            log.warning("attempt %d failed (%s: %s) — again in %.0fs", attempt, type(e).__name__, str(e)[:160], pause)
            if on_wait:
                try:
                    on_wait(e, attempt, pause)
                except Exception:
                    pass
            await sleep(pause)


# ================= Keys =================

def estimate_tokens(kw: dict) -> int:
    """A rough size of a request in tokens (≈ 3.5 characters each), to budget daily token limits."""
    try:
        size = len(json.dumps(kw.get("messages") or [], ensure_ascii=False, default=str))
        size += len(json.dumps(kw.get("tools") or [], ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        size = 0
    return int(size / 3.5)


class UsageStore:
    """Daily request times (and tokens) per key — by a hash, never the key — on disk, so limits survive restarts."""

    def __init__(self, path: Path | None):
        self.path = path
        self._data: dict = {}
        self._flushed_at = 0.0
        self._dirty = False
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._data = data if isinstance(data, dict) else {}
            except Exception:
                self._data = {}

    def load(self, pool: str, key_id: str) -> list:
        value = self._data.get(pool, {}).get(key_id, [])
        return list(value) if isinstance(value, list) else []

    def save(self, pool: str, key_id: str, items) -> None:
        if not self.path:
            return
        self._data.setdefault(pool, {})[key_id] = [
            [round(x[0], 1), *(int(v) for v in x[1:])] if isinstance(x, (list, tuple)) else round(x, 1) for x in items]
        self._dirty = True
        if time.time() - self._flushed_at > 5:  # at most every 5 s (and on shutdown)
            self.flush()

    def flush(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self.path)
            self._flushed_at, self._dirty = time.time(), False
        except OSError:
            pass


@dataclass
class Slot:
    name: str                      # "key 1", "key 2"… — never the key itself
    client: object
    index: int = 0
    key_id: str = ""
    recent: deque = field(default_factory=deque)   # start times of recent requests (monotonic)
    day: deque = field(default_factory=deque)      # start times in the last 24 h (wall clock)
    tok: deque = field(default_factory=deque)      # (wall time, input tokens, output tokens) in the last 24 h
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

    def today(self, wall: float) -> int:
        while self.day and wall - self.day[0] >= DAY:
            self.day.popleft()
        return len(self.day)

    def tokens_io(self, wall: float) -> tuple[int, int]:
        while self.tok and wall - self.tok[0][0] >= DAY:
            self.tok.popleft()
        return sum(x[1] for x in self.tok), sum(x[2] for x in self.tok)

    def tokens(self, wall: float) -> int:
        return sum(self.tokens_io(wall))


def _retry_after(e: APIStatusError) -> float | None:
    try:
        value = e.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


def _is_quota(e: APIStatusError) -> bool:
    text = f"{getattr(e, 'message', '')} {getattr(e, 'body', '')}".lower()
    return any(w in text for w in ("per day", "per-day", "daily", "quota", "today", "/day", "insufficient"))


class _Metered:
    """Wraps a streamed reply to count its tokens for the key that served it (usage if sent, else an estimate)."""

    def __init__(self, stream, prompt_estimate: int, on_done):
        self._s, self._prompt, self._on_done = stream, prompt_estimate, on_done
        self._chars, self._usage, self._started, self._reported = 0, None, False, False   # usage: (in, out)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._s.__anext__()
        except StopAsyncIteration:
            self._report()
            raise
        self._started = True
        u = getattr(chunk, "usage", None)
        if u is not None and (getattr(u, "prompt_tokens", None) or getattr(u, "completion_tokens", None)):
            self._usage = (int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0))
        for c in getattr(chunk, "choices", None) or []:
            d = getattr(c, "delta", None)
            if d is not None:
                extra = getattr(d, "model_extra", None) or {}
                self._chars += len(getattr(d, "content", None) or "") + len(
                    str(extra.get("reasoning") or extra.get("reasoning_content") or ""))
                for tc in getattr(d, "tool_calls", None) or []:
                    fn = getattr(tc, "function", None)
                    self._chars += len(getattr(fn, "arguments", None) or "") if fn else 0
        return chunk

    def _report(self) -> None:
        if self._reported:
            return
        self._reported = True
        if self._usage is not None:
            self._on_done(*self._usage)
        elif self._started:  # no usage sent: the request's size, and what came back
            self._on_done(self._prompt, int(self._chars / 3.5))

    async def close(self):
        self._report()
        if hasattr(self._s, "close"):
            try:
                await self._s.close()
            except Exception:
                pass


class KeyPool:
    def __init__(self, keys: list[str], base_url: str, rpm_per_key: int = 40, max_wait: float = 45.0,
                 client_factory=None, clock=time.monotonic, name: str = "NVIDIA", rpd_per_key: int = 0,
                 usage_store: UsageStore | None = None, wall=time.time, secret: str = "NVIDIA_KEYS",
                 tpd_per_key: int = 0, tpd_in_per_key: int = 0, tpd_out_per_key: int = 0):
        # a short connect timeout, and at most 3 minutes of silence between chunks (instead of the SDK's 10)
        factory = client_factory or (lambda key: AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0,
                                                             timeout=httpx.Timeout(180.0, connect=15.0)))
        self.name, self.secret = name, secret
        self.rpm = max(1, int(rpm_per_key))
        self.rpd = max(0, int(rpd_per_key or 0))
        self.tpd = max(0, int(tpd_per_key or 0))            # tokens a day (input + output)
        self.tpd_in = max(0, int(tpd_in_per_key or 0))      # input tokens a day
        self.tpd_out = max(0, int(tpd_out_per_key or 0))    # output tokens a day
        self.metered = bool(self.tpd or self.tpd_in or self.tpd_out)
        self.max_wait = max_wait
        self.clock, self.wall = clock, wall
        self.store = usage_store
        self.slots = []
        now = self.wall()
        for i, k in enumerate(dict.fromkeys(k for k in keys if k)):
            key_id = hashlib.sha256(k.encode()).hexdigest()[:12]
            s = Slot(f"key {i + 1}", factory(k), i, key_id)
            if self.store and self.rpd:
                s.day.extend(t for t in self.store.load(self.name, key_id)
                             if isinstance(t, (int, float)) and now - t < DAY)
            if self.store and self.metered:
                for x in self.store.load(self.name + ":tokens", key_id):
                    if isinstance(x, list) and len(x) in (2, 3) and now - x[0] < DAY:
                        s.tok.append((x[0], int(x[1]), int(x[2]) if len(x) == 3 else 0))
            self.slots.append(s)
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self.slots)

    def _token_use(self, s: Slot, wall: float) -> list[float]:
        """Today's share used of each daily token budget that's set (1.0 = used up)."""
        if not self.metered:
            return []
        i, o = s.tokens_io(wall)
        return ([(i + o) / self.tpd] if self.tpd else []) + ([i / self.tpd_in] if self.tpd_in else []) + (
            [o / self.tpd_out] if self.tpd_out else [])

    def _spent(self, s: Slot, wall: float) -> bool:
        return (bool(self.rpd) and s.today(wall) >= self.rpd) or any(u >= 1 for u in self._token_use(s, wall))

    def _usable(self, s: Slot, now: float, wall: float) -> bool:
        return (now >= s.dead_until and now >= s.cooldown_until and s.load(now) < self.rpm
                and not self._spent(s, wall))

    def pick(self) -> Slot | None:
        """The key with the fewest requests in the last minute, then the most daily quota left, then the earlier line."""
        now, wall = self.clock(), self.wall()
        usable = [s for s in self.slots if self._usable(s, now, wall)]
        if not usable:
            return None

        def used_today(s: Slot) -> float:
            parts = ([s.today(wall) / self.rpd] if self.rpd else []) + self._token_use(s, wall)
            return max(parts) if parts else 0.0

        return min(usable, key=lambda s: (s.load(now), round(used_today(s), 2), s.index))

    def next_free_in(self) -> float | None:
        """Seconds until some key can take a request again (None: every key is rejected)."""
        now, wall = self.clock(), self.wall()
        waits = []
        for s in self.slots:
            if now < s.dead_until:
                continue
            w = max(0.0, s.cooldown_until - now)
            if s.load(now) >= self.rpm:
                w = max(w, WINDOW - (now - s.recent[0]))
            if self.rpd and s.today(wall) >= self.rpd:
                w = max(w, DAY - (wall - s.day[0]))
            if self.metered and s.tok and any(u >= 1 for u in self._token_use(s, wall)):
                w = max(w, DAY - (wall - s.tok[0][0]))
            waits.append(w)
        return min(waits) if waits else None

    _next_free_in = next_free_in  # older name

    def day_left(self) -> float | None:
        """Share of today's quota left over the working keys (requests or tokens, whichever is scarcer)."""
        now, wall = self.clock(), self.wall()
        live = [s for s in self.slots if now >= s.dead_until]
        if not live or not (self.rpd or self.metered):
            return None
        parts = []
        if self.rpd:
            parts.append(sum(max(0, self.rpd - s.today(wall)) for s in live) / (len(live) * self.rpd))
        if self.metered:
            per_budget = zip(*(self._token_use(s, wall) for s in live))
            parts += [sum(max(0.0, 1 - u) for u in col) / len(live) for col in per_budget]
        return min(parts)

    def tokens_left(self) -> int | None:
        """Input tokens a request may still use today on the best working key (None: no token limit)."""
        if not (self.tpd or self.tpd_in):
            return None
        now, wall = self.clock(), self.wall()
        best = 0
        for s in self.slots:
            if now < s.dead_until:
                continue
            i, o = s.tokens_io(wall)
            left = [b for b in ((self.tpd - i - o) if self.tpd else None, (self.tpd_in - i) if self.tpd_in else None)
                    if b is not None]
            best = max(best, max(0, min(left)))
        return best

    def capacity(self) -> dict:
        """How busy the pool is: can a key take a request now, how long until one can, how full, quota left."""
        now, wall = self.clock(), self.wall()
        live = [s for s in self.slots if now >= s.dead_until]
        free_now = any(self._usable(s, now, wall) for s in self.slots)
        used = sum(min(s.load(now), self.rpm) for s in live) / max(1, len(live) * self.rpm)
        nfi = None if free_now else self.next_free_in()
        wait = 0.0 if free_now else (nfi if nfi is not None else DAY)
        return {"free_now": free_now, "wait": wait, "used": used, "day_left": self.day_left(), "live": len(live),
                "out": not free_now and nfi is None, "tokens_left": self.tokens_left()}

    def _count(self, slot: Slot) -> None:
        now = self.clock()
        slot.recent.append(now)
        slot.last_used = now
        slot.total += 1
        if self.rpd:
            slot.day.append(self.wall())
            if self.store:
                self.store.save(self.name, slot.key_id, slot.day)

    def _add_tokens(self, slot: Slot, n_in: int, n_out: int = 0) -> None:
        if not self.metered or n_in + n_out <= 0:
            return
        slot.tok.append((self.wall(), int(n_in), int(n_out)))
        if self.store:
            self.store.save(self.name + ":tokens", slot.key_id, slot.tok)

    async def _take(self, max_wait: float | None = None) -> Slot:
        deadline = self.clock() + (self.max_wait if max_wait is None else max_wait)
        while True:
            async with self._lock:
                slot = self.pick()
                if slot:
                    self._count(slot)
                    return slot
                wait = self.next_free_in()
            if not self.slots:
                raise NoKeyAvailable(f"No {self.name} keys are set (add the {self.secret} secret, one key per line).")
            if wait is None:
                raise NoKeyAvailable(f"Every {self.name} key was rejected — check or replace them ({self.secret}).")
            if self.clock() + wait > deadline:
                if wait > 3600:
                    raise NoKeyAvailable(f"All {self.name} keys used up today's limits — they reset within "
                                         f"{wait / 3600:.0f} h.")
                raise NoKeyAvailable(f"All {self.name} keys are at their limits right now — try again shortly.")
            await asyncio.sleep(min(max(wait, 0.05), 5.0))

    async def create(self, max_wait: float | None = None, **kw):
        """One request through the pool, moving to another key on 429 / auth / server errors."""
        last: Exception | None = None
        prompt = estimate_tokens(kw) if self.metered else 0
        for _ in range(max(2, 2 * len(self.slots))):
            try:
                slot = await self._take(max_wait)
            except NoKeyAvailable:
                if last is not None:   # every key just failed: the real reason, not "they're resting"
                    raise last from None
                raise
            try:
                res = await slot.client.chat.completions.create(**kw)
                slot.last_status = "ok"
                if self.metered:
                    if kw.get("stream"):
                        return _Metered(res, prompt, lambda i, o, s=slot: self._add_tokens(s, i, o))
                    u = getattr(res, "usage", None)
                    self._add_tokens(slot, int(getattr(u, "prompt_tokens", 0) or 0) or prompt,
                                     int(getattr(u, "completion_tokens", 0) or 0))
                return res
            except APIStatusError as e:
                last = e
                slot.failures += 1
                code = e.status_code
                slot.last_status = f"HTTP {code}"
                if code == 429:
                    quota = _is_quota(e)
                    slot.cooldown_until = self.clock() + (COOLDOWN_QUOTA if quota else
                                                          (_retry_after(e) or COOLDOWN_429))
                    if quota:
                        slot.last_status = "daily limit reached"
                elif code in (401, 402, 403):
                    slot.dead_until = self.clock() + DEAD_AUTH
                elif code >= 500:
                    slot.cooldown_until = self.clock() + COOLDOWN_ERROR
                else:
                    raise  # a bad request fails the same on every key
                log.warning("%s %s answered %s — trying another key", self.name, slot.name, code)
            except (APIConnectionError, APITimeoutError) as e:
                last = e
                slot.failures += 1
                slot.last_status = "connection error"
                slot.cooldown_until = self.clock() + COOLDOWN_ERROR
        raise last or NoKeyAvailable(f"No {self.name} key could take the request")

    async def ping(self, slot: Slot, model: str, timeout: float = 30.0, extra_body: dict | None = None
                   ) -> tuple[bool | None, str, float]:
        """Checks one key in two steps, so a slow model is never mistaken for a bad key:
        1) the request is accepted (a wrong key is refused within a second: 401/403),
        2) the first streamed token arrives (a free service may queue requests for a while).
        Returns (True = works / None = key accepted but the model is slow / False = broken, status, seconds)."""
        start = self.clock()
        self._count(slot)
        stream = None
        try:
            stream = await asyncio.wait_for(slot.client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=8, stream=True,
                **({"extra_body": extra_body} if extra_body else {})), timeout)
            accepted = self.clock() - start
            try:
                await asyncio.wait_for(first_chunks(stream), max(5.0, timeout * 2 - accepted))
            except asyncio.TimeoutError:
                slot.last_status = "slow"
                return None, "key accepted · model busy (queued)", self.clock() - start
            slot.last_status = "ok"
            self._add_tokens(slot, 12, 8)
            return True, f"answers · first token {self.clock() - start:.1f}s", self.clock() - start
        except APIStatusError as e:
            slot.last_status = f"HTTP {e.status_code}"
            meaning = {401: "key rejected (401)", 402: "no credit (402)", 403: "key rejected (403)",
                       429: "rate limited right now (429)",
                       404: "model not found (404)"}.get(e.status_code, f"HTTP {e.status_code}")
            if e.status_code == 429:
                quota = _is_quota(e)
                slot.cooldown_until = self.clock() + (COOLDOWN_QUOTA if quota else (_retry_after(e) or COOLDOWN_429))
                if quota:
                    meaning = "daily limit reached (429)"
            elif e.status_code in (401, 402, 403):
                slot.dead_until = self.clock() + DEAD_AUTH
            return False, meaning, self.clock() - start
        except asyncio.TimeoutError:
            slot.last_status = "timeout"
            return False, f"no response in {timeout:.0f}s", self.clock() - start
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
        now, wall = self.clock(), self.wall()
        out = []
        for s in self.slots:
            spent = self._spent(s, wall)
            state = ("rejected" if now < s.dead_until else
                     f"resting {s.cooldown_until - now:.0f}s" if now < s.cooldown_until else
                     "out for today" if spent else "ready")
            out.append({"name": s.name, "state": state, "last_minute": s.load(now), "today": s.today(wall),
                        "tokens": s.tokens(wall), "tokens_in": s.tokens_io(wall)[0],
                        "tokens_out": s.tokens_io(wall)[1], "total": s.total, "failures": s.failures,
                        "last": s.last_status})
        return out


# ================= Providers =================

def to_free(kw: dict, models: dict, model_extra: dict) -> dict:
    """An OpenRouter-shaped request → a plain OpenAI one for a free provider: its model id, no OpenRouter fields."""
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


to_nvidia = to_free  # older name


class Provider:
    def __init__(self, name: str, label: str, paid: bool, models: dict | None = None, model_extra: dict | None = None,
                 pool: KeyPool | None = None, client=None, first_token_timeout: float = 120.0, attempts: int = 2,
                 catalog_url: str = ""):
        self.name, self.label, self.paid = name, label, paid
        self.models = {k: v for k, v in (models or {}).items() if not str(k).startswith("_")}
        self.model_extra = {k: v for k, v in (model_extra or {}).items() if not str(k).startswith("_")}
        self.pool, self.client = pool, client
        self.first_token_timeout, self.attempts = first_token_timeout, max(1, attempts)
        self.stream_usage = True
        # a source whose models come and go (e.g. OpenRouter's ":free" versions): only the ids its public model list
        # shows right now are served — none until the first check (None = no list to check: always served)
        self.catalog_url = catalog_url
        self.live_ids: set | None = set() if catalog_url else None

    @property
    def active(self) -> bool:
        return self.client is not None if self.pool is None else len(self.pool) > 0

    def serves(self, model: str) -> bool:
        if self.paid and self.pool is None:
            return True
        return model in self.models and (self.live_ids is None or self.models[model] in self.live_ids)

    def waiting_for(self) -> list[str]:
        """Model ids this source is set up for but doesn't offer right now (per its model list)."""
        if self.live_ids is None:
            return []
        return [v for v in self.models.values() if v not in self.live_ids]

    def model_id(self, model: str) -> str:
        return self.models.get(model, model)

    def request(self, kw: dict) -> dict:
        if self.pool is None:
            return dict(kw)
        req = to_free(kw, self.models, self.model_extra)
        if req.get("stream") and self.stream_usage and "stream_options" not in req:
            req["stream_options"] = {"include_usage": True}   # token counts for daily budgets
        return req

    async def create(self, kw: dict, max_wait: float | None = None):
        req = self.request(kw)
        if self.pool is None:
            return await self.client.chat.completions.create(**req)
        try:
            return await self.pool.create(max_wait=max_wait, **req)
        except APIStatusError as e:
            if e.status_code in (400, 422) and "stream_options" in req and "stream_options" in str(e).lower():
                self.stream_usage = False   # this provider doesn't take it: go on without
                req.pop("stream_options", None)
                return await self.pool.create(max_wait=max_wait, **req)
            raise

    def describe(self) -> str:
        if self.paid:
            return f"{self.label} (paid)"
        n = len(self.pool) if self.pool is not None else 0
        return f"{self.label} (free · {n} key{'s' if n != 1 else ''})"


# ================= Health =================

@dataclass
class Health:
    ttft: float | None = None      # moving average of seconds to the first token
    fails: float = 0.0             # moving failure rate, 0..1
    streak: int = 0                # failures in a row
    open_until: float = 0.0        # circuit breaker: not tried before this (unless nothing else is left)
    last: float = 0.0              # when it was last observed
    n: int = 0                     # observations

    def expected(self, now: float) -> float:
        """Expected seconds to the first token; old knowledge fades back toward the prior."""
        if self.ttft is None:
            return PRIOR_TTFT
        age = now - self.last
        if age > 600:  # after 10 minutes, half-forget (a slow hour shouldn't ban a provider forever)
            w = 0.5 if age < 3600 else 0.8
            return self.ttft * (1 - w) + PRIOR_TTFT * w
        return self.ttft


class Stats:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.h: dict[tuple, Health] = {}

    def get(self, provider: str, model: str) -> Health:
        return self.h.setdefault((provider, model), Health())

    def ok(self, provider: str, model: str, ttft: float) -> None:
        h = self.get(provider, model)
        h.ttft = ttft if h.ttft is None else 0.7 * h.ttft + 0.3 * ttft
        h.fails *= 0.7
        h.streak, h.open_until, h.last, h.n = 0, 0.0, self.clock(), h.n + 1

    def slow(self, provider: str, model: str, waited: float) -> None:
        """It hadn't answered after `waited` seconds (another provider won, or the deadline passed)."""
        h = self.get(provider, model)
        base = PRIOR_TTFT if h.ttft is None else h.ttft
        if waited <= base:  # cancelled before its usual time: that says nothing about it
            return
        h.ttft = 0.5 * base + 0.5 * waited
        h.last, h.n = self.clock(), h.n + 1

    def fail(self, provider: str, model: str) -> None:
        h = self.get(provider, model)
        h.fails = 0.7 * h.fails + 0.3
        h.streak += 1
        h.last, h.n = self.clock(), h.n + 1
        if h.streak >= 2:
            h.open_until = self.clock() + min(600.0, 30.0 * 2 ** (h.streak - 2))

    def summary(self) -> list[str]:
        now = self.clock()
        rows = []
        for (p, m), h in sorted(self.h.items()):
            if not h.n:
                continue
            state = f"paused {h.open_until - now:.0f}s" if h.open_until > now else "ok"
            ttft = f"{h.ttft:.1f}s" if h.ttft is not None else "—"
            rows.append(f"{p} · {m}: first token ~{ttft} · failure rate {h.fails:.0%} · {state}")
        return rows


# ================= Router =================

class ModelRouter:
    """Looks like an OpenAI client. mode_fn() picks "smart" or a provider's name for every request."""

    def __init__(self, providers: dict, mode_fn, paid_fallback_fn=None, stats: Stats | None = None,
                 hedge_min: float = 10.0, hedge_max: float = 45.0, deadline: float = 150.0, clock=time.monotonic,
                 picked_patience: float = 40.0):
        self.providers = providers                      # name → Provider (OpenRouter first)
        self.mode_fn = mode_fn
        self.paid_fallback_fn = paid_fallback_fn or (lambda: False)
        self.stats = stats or Stats(clock)
        self.hedge_min, self.hedge_max, self.deadline = hedge_min, hedge_max, deadline
        self.picked_patience = picked_patience   # a picked source's head start before another one joins
        self.clock = clock
        self.affinity: "OrderedDict[str, str]" = OrderedDict()
        self.last_served = ""
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    # ----- choices -----
    def mode(self) -> str:
        m = str(self.mode_fn() or "openrouter")
        return m if m == "smart" or m in self.providers else "openrouter"

    def options(self, model: str) -> list[str]:
        """What the owner can pick for this model: smart (if any free provider serves it) + every provider that can."""
        names = [n for n, p in self.providers.items() if p.serves(model) and (p.paid or p.active)]
        free = [n for n in names if not self.providers[n].paid]
        return (["smart"] if free else []) + names

    def free_for(self, model: str) -> list["Provider"]:
        return [p for p in self.providers.values() if not p.paid and p.active and p.serves(model)]

    def describe(self, model: str) -> str:
        mode = self.mode()
        if mode != "smart":
            p = self.providers[mode]
            return p.describe() + ("" if p.serves(model) else f" — doesn't serve {model}")
        free = [p.label for p in self.free_for(model)]
        tail = ", then paid" if self.paid_fallback_fn() else ""
        return f"Smart — free first ({', '.join(free) or 'no free provider has keys'}){tail}"

    # ----- ranking -----
    def rank(self, model: str, need: int = 0) -> list["Provider"]:
        """Free providers for this model, best first; then (optionally) the paid one as the last resort.
        need: the request's rough size in tokens (for providers with a daily token budget)."""
        now = self.clock()
        key = ROUTE_KEY.get()
        sticky = self.affinity.get(key) if key else None
        order = {n: i for i, n in enumerate(self.providers)}
        scored, paused, out_now = [], [], []
        for p in self.free_for(model):
            h = self.stats.get(p.name, model)
            cap = p.pool.capacity()
            tokens_left = cap["tokens_left"]
            if cap["out"] or (tokens_left is not None and need > tokens_left):
                out_now.append(p)              # every key rejected / not enough budget left today: last
                continue
            score = h.expected(now) * (1 + 3 * h.fails)
            if not cap["free_now"]:
                score += 2 * cap["wait"]       # a key frees up only later
            score *= 1 + 0.5 * cap["used"]     # spread the load
            if cap["day_left"] is not None and cap["day_left"] < 0.15:
                score *= 1.5                   # save the last of a daily quota
            if tokens_left:
                score *= 1 + 2 * need / tokens_left   # a big request on a small budget: prefer elsewhere
            if sticky == p.name:
                score *= 0.6                   # same conversation, same provider (its cache, steady behaviour)
            (paused if h.open_until > now else scored).append((score, order[p.name], p))
        scored.sort(key=lambda x: (x[0], x[1]))
        # providers in a cool-down only if nothing else is left (a probe), soonest-back first
        paused.sort(key=lambda x: (self.stats.get(x[2].name, model).open_until, x[1]))
        out = [p for *_, p in scored] + [p for *_, p in paused] + out_now
        if self.paid_fallback_fn():
            out += [p for p in self.providers.values() if p.paid and p.active and p.serves(model)]
        return out

    def _hedge_delay(self, p: "Provider", model: str) -> float:
        exp = self.stats.get(p.name, model).expected(self.clock())
        return max(self.hedge_min, min(self.hedge_max, 1.5 * exp))

    def _remember(self, p: "Provider") -> None:
        self.last_served = p.name
        key = ROUTE_KEY.get()
        if key:
            self.affinity[key] = p.name
            self.affinity.move_to_end(key)
            while len(self.affinity) > 300:
                self.affinity.popitem(last=False)

    def _wrap(self, p: "Provider", model: str, stream, first):
        return PrefetchedStream(first, stream, on_error=lambda e: self.stats.fail(p.name, model))

    @staticmethod
    def _busy(e: Exception) -> bool:
        """All keys at their limits right now — busy, not broken."""
        return isinstance(e, NoKeyAvailable) and not isinstance(e, (ProviderSlow, NoRoute)) and "limits" in str(e)

    # ----- requests -----
    async def _create(self, **kw):
        model = kw.get("model", "")
        mode = self.mode()
        if mode == "smart":
            return await self._smart(kw, model)
        return await self._pinned(self.providers[mode], kw, model)

    def _rescue(self, p: "Provider", model: str, need: int) -> list:
        """Who can stand in for a picked source: the other sources for this model, best first."""
        return [q for q in self.rank(model, need) if q is not p]

    async def _pinned(self, p: "Provider", kw: dict, model: str):
        """A picked source goes first. If it fails twice in a row it rests (30 s, then longer) and the next best
        source stands in until it's back; if it stays silent past its first-token time, another source starts
        alongside it and the first to answer wins. A single failure is retried on it (by the caller, after a pause)."""
        if not p.active:
            raise NoKeyAvailable(f"{p.label} is selected but has no keys — add the "
                                 f"{p.pool.secret if p.pool is not None else 'API key'} secret, or pick another source in Settings.")
        if not p.serves(model):
            raise NoRoute(f"{p.label} doesn't serve {model} — pick another source in Settings.")
        need = estimate_tokens(kw)
        rescue = self._rescue(p, model, need)
        resting = self.stats.get(p.name, model).open_until > self.clock()
        if resting and rescue:
            _emit(f"!{p.label} failed twice in a row — using {rescue[0].label} until it recovers")
        if not kw.get("stream"):
            for q in (rescue + [p] if resting and rescue else [p] + rescue):
                t0 = self.clock()
                try:
                    res = await q.create(kw)
                except Exception as e:
                    if not self._busy(e):
                        self.stats.fail(q.name, model)
                    if not retryable(e) or (q is p and self.stats.get(p.name, model).open_until <= self.clock()):
                        raise
                    continue
                self.stats.ok(q.name, model, self.clock() - t0)
                self._remember(q)
                return res
            raise NoRoute(f"{p.label} and every other source failed.")
        if p.pool is not None and not resting:
            _emit(f"Waiting for {p.label} (free) — its queue can be slow")
        if resting and rescue:
            return await self._race(rescue + [p], kw, model)
        return await self._race([p] + rescue, kw, model, first_hedge=min(self.picked_patience, p.first_token_timeout),
                                deadline=p.first_token_timeout + (self.deadline if rescue else 0), strict_first=True)

    def _no_route(self, model: str) -> NoRoute:
        if any(p.paid and p.serves(model) for p in self.providers.values()):
            return NoRoute(f"Smart mode: no free provider with keys serves {model}. Add keys (NVIDIA_KEYS, "
                           "AIHUBMIX_KEYS…), turn on 'Paid as last resort', or pick OpenRouter in Settings.")
        return NoRoute(f"Nothing serves {model}.")

    @staticmethod
    def _combined(errors: list) -> Exception:
        """One error for the owner when every candidate failed: a bad request as it is, otherwise a short list."""
        bad = [e for _, e in errors if isinstance(e, APIStatusError) and e.status_code in (400, 413, 422)]
        if bad and len(bad) == len(errors):   # the request itself was refused everywhere
            return bad[0]
        if len(errors) == 1:
            return errors[0][1]
        parts = "; ".join(f"{p.label}: {str(getattr(e, 'message', '') or e)[:90] or type(e).__name__}"
                          for p, e in errors)
        err = NoRoute("Every source failed — " + parts)
        err.retry = any(retryable(e) for _, e in errors)   # worth another try if any of them was temporary
        return err

    async def _smart(self, kw: dict, model: str):
        cands = self.rank(model, estimate_tokens(kw))
        if not cands:
            raise self._no_route(model)
        if not kw.get("stream"):  # plain requests: one after another, no racing
            errors: list = []
            for p in cands:
                t0 = self.clock()
                try:
                    res = await p.create(kw)
                except Exception as e:
                    errors.append((p, e))
                    if not self._busy(e):
                        self.stats.fail(p.name, model)
                    continue
                self.stats.ok(p.name, model, self.clock() - t0)
                self._remember(p)
                return res
            raise self._combined(errors)
        return await self._race(cands, kw, model)

    async def _race(self, cands: list, kw: dict, model: str, first_hedge: float | None = None,
                    deadline: float | None = None, strict_first: bool = False):
        """Start the best candidate; if it's silent too long, start the next one too; the first to answer wins.
        An error moves on at once. The paid provider (if allowed) starts only when every free one failed or the
        deadline passed. Picked source (strict_first): it gets first_hedge seconds before anyone joins, and its
        first failure is handed back to the caller (to retry it) — only a second one in a row moves on."""
        lead = cands[0]
        if strict_first:   # the picked source leads, whatever it is; the rest follow (paid ones only if allowed)
            free, paid = [lead] + [p for p in cands[1:] if not p.paid], [p for p in cands[1:] if p.paid]
        else:
            free, paid = [p for p in cands if not p.paid], [p for p in cands if p.paid]
        deadline = self.clock() + (self.deadline if deadline is None else deadline)
        launched = 0
        pending: dict = {}           # task → (provider, started_at)
        errors: list = []            # (provider, exception)
        timed_out = False

        async def open_one(p: "Provider", max_wait: float | None):
            stream = await p.create(kw, max_wait=max_wait)
            try:
                first = await first_chunks(stream)
            except BaseException:
                await _close(stream)
                raise
            return stream, first

        def launch(p: "Provider") -> None:
            nonlocal deadline, launched
            launched += 1
            if p.paid and not (strict_first and p is lead):   # the last resort gets its full first-token time
                deadline = max(deadline, self.clock() + p.first_token_timeout)
            # a busy provider may wait for a key to free up (a hedge starts the next one meanwhile)
            max_wait = max(2.0, min(p.pool.max_wait if p.pool is not None else 0.0, deadline - self.clock()))
            task = asyncio.ensure_future(open_one(p, max_wait))
            pending[task] = (p, self.clock())
            names = [q.label for q, _ in pending.values()]
            _emit(f"Waiting for {names[0]}" + (f" — also trying {', '.join(names[1:])}" if len(names) > 1 else ""))

        def hedge_at() -> float:
            newest = list(pending.values())[-1]
            if first_hedge is not None and launched == 1:
                return newest[1] + first_hedge
            return newest[1] + self._hedge_delay(newest[0], model)

        try:
            launch((free or paid).pop(0))
            while pending or free or paid:
                if not pending:          # everything so far failed: the next one right away
                    launch((free or paid).pop(0))
                now = self.clock()
                if now >= deadline:
                    if not paid:
                        timed_out = True
                        break
                    free.clear()         # the paid last resort still gets its chance (free ones keep racing)
                    launch(paid.pop(0))
                    continue
                until = min(deadline, hedge_at()) if free else deadline
                done, _ = await asyncio.wait(list(pending), timeout=max(0.0, until - now),
                                             return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    p, started = pending.pop(task)
                    exc = task.exception()
                    if exc is not None:
                        errors.append((p, exc))
                        if not self._busy(exc):
                            self.stats.fail(p.name, model)
                        log.warning("%s failed (%s: %s) — moving on", p.label, type(exc).__name__, str(exc)[:160])
                        if strict_first and p is lead:
                            busy, resting = self._busy(exc), self.stats.get(p.name, model).open_until > self.clock()
                            if not pending and retryable(exc) and not busy and not resting:
                                raise exc   # once: the caller tries the picked source again after a pause
                            if free or paid or pending:
                                why = ("is at its limits" if busy else "failed twice in a row" if resting
                                       else "can't take this request")
                                _emit(f"!{p.label} {why} — moving on")
                        continue
                    stream, first = task.result()
                    self.stats.ok(p.name, model, self.clock() - started)
                    await self._cancel_rest(pending, model)
                    self._remember(p)
                    if len(cands) > 1:
                        _emit(f"via {p.label}")
                    return self._wrap(p, model, stream, first if first else StopAsyncIteration)
                if not done and free and pending and self.clock() >= hedge_at():
                    launch(free.pop(0))
        finally:
            if timed_out:   # silent all the way to the deadline: that's a failure, not just slowness
                for p, _ in pending.values():
                    self.stats.fail(p.name, model)
            await self._cancel_rest(pending, model)
        if errors and not timed_out:
            raise self._combined(errors)
        tried = ", ".join(dict.fromkeys(p.label for p in cands))
        if strict_first and len(cands) == 1:
            raise ProviderSlow(f"{lead.label} accepted the request but didn't start answering in "
                               f"{lead.first_token_timeout:.0f}s (its queue is busy). Trying again; Smart or "
                               "another source in Settings avoids the wait.")
        raise ProviderSlow(f"No source started answering in time ({tried}). Trying again; 'Paid as last resort' "
                           "in Settings avoids the wait.")

    async def _cancel_rest(self, pending: dict, model: str) -> None:
        for task, (p, started) in list(pending.items()):
            task.cancel()
            self.stats.slow(p.name, model, self.clock() - started)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            # a task that finished just before being cancelled still holds an open stream
            for task in pending:
                if task.done() and not task.cancelled() and task.exception() is None:
                    await _close(task.result()[0])
        pending.clear()


async def _close(stream) -> None:
    if stream is not None and hasattr(stream, "close"):
        try:
            await stream.close()
        except Exception:
            pass


async def refresh_catalogs(providers: dict, fetch=None) -> list[str]:
    """Checks the public model list of every source that has one; returns what changed ("OpenRouter Free: now
    offers …" / "no longer offers …"). A failed check keeps what was known."""
    changes = []
    for p in providers.values():
        if not p.catalog_url:
            continue
        try:
            if fetch:
                data = await fetch(p.catalog_url)
            else:
                async with httpx.AsyncClient(timeout=30) as http:
                    r = await http.get(p.catalog_url)
                    r.raise_for_status()
                    data = r.json()
            ids = {str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)}
        except Exception as e:
            log.warning("couldn't check %s's model list: %s", p.label, type(e).__name__)
            continue
        now = ids & set(p.models.values())
        before = p.live_ids or set()
        for m in sorted(now - before):
            changes.append(f"{p.label}: now offers {m}")
        for m in sorted(before - now):
            changes.append(f"{p.label}: no longer offers {m}")
        p.live_ids = now
    return changes


def build_providers(openrouter_client, free_cfg: dict, env: dict, usage_store: UsageStore | None = None,
                    routing: dict | None = None, client_factory=None) -> dict:
    """OpenRouter first, then every configured free provider (with or without keys)."""
    routing = routing or {}
    out = {"openrouter": Provider("openrouter", "OpenRouter", True, client=openrouter_client)}
    for name, c in free_cfg.items():
        if name.startswith("_") or not isinstance(c, dict):
            continue
        secret = c.get("keys_secret") or f"{name.upper()}_KEYS"
        raw = [env.get(n, "") for n in [secret, *(c.get("legacy_env") or [])]]
        keys = parse_keys(*raw)
        pool = KeyPool(keys, c.get("base_url", ""), int(c.get("rpm_per_key", 20)),
                       float(routing.get("max_wait_seconds", 45)), client_factory, name=c.get("label", name),
                       rpd_per_key=int(c.get("rpd_per_key", 0) or 0), usage_store=usage_store, secret=secret,
                       tpd_per_key=int(c.get("tpd_per_key", 0) or 0),
                       tpd_in_per_key=int(c.get("tpd_in_per_key", 0) or 0),
                       tpd_out_per_key=int(c.get("tpd_out_per_key", 0) or 0))
        out[name] = Provider(name, c.get("label", name), False, c.get("models"), c.get("model_extra"), pool=pool,
                             first_token_timeout=float(routing.get("first_token_timeout", 120)),
                             attempts=int(routing.get("attempts", 2)), catalog_url=c.get("catalog_url", ""))
    return out

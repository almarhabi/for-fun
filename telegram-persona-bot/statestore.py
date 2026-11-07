"""A durable copy of the bot's important state, kept outside the Actions cache.

The Actions cache can come back empty (a new run may start before the previous run's save is visible), so the files
that must never be lost — the persona, the lock rules, the Coding identities, the settings, the title, the snippets —
are mirrored into the repository's Actions *variables* (private: only people with admin access to the repo can read
them, never shown publicly), encrypted with PERSONA_KEY.

- Every write of a mirrored file schedules an upload (debounced 2 s, in a background thread).
- On start, the newer of the local file and the stored copy wins, so a run with an empty cache gets everything back.
- Needs GH_PAT (repo scope) and GITHUB_REPOSITORY (set automatically on GitHub Actions); otherwise it's off.
- Values are split into chunks (GitHub limits a variable to 48 KB); names: <PREFIX>_<FILE>_<n>.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import httpx

import vault

log = logging.getLogger("persona-bot.state")

# file → largest size worth mirroring (characters, before encryption)
MIRRORED = {"persona.json": 60_000, "rules.json": 60_000, "lead_persona.json": 30_000,
            "agents_persona.json": 30_000, "settings.json": 8_000, "meta.json": 4_000,
            "snippets.json": 24_000, "telegraph.json": 4_000}
CHUNK = 40_000
HEADER = re.compile(r"^v1:(\d+):(\d+(?:\.\d+)?):")


def _slug(filename: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", filename.rsplit(".", 1)[0].upper())


class StateStore:
    def __init__(self, repo: str = "", token: str = "", prefix: str = "BOTSTATE", api_base: str = "",
                 delay: float = 2.0):
        self.repo = (repo or os.getenv("GITHUB_REPOSITORY", "")).strip()
        self.token = (token or os.getenv("GH_PAT", "") or os.getenv("GITHUB_PAT", "")).strip().strip("\"'")
        self.prefix = re.sub(r"[^A-Z0-9_]", "_", (prefix or os.getenv("BOT_STATE_PREFIX", "") or "BOTSTATE").upper())
        self.api = (api_base or os.getenv("GITHUB_API_BASE", "") or "https://api.github.com").rstrip("/")
        self.delay = delay
        self.pending: dict[str, str] = {}
        self.chunks: dict[str, int] = {}      # slug → chunks stored now (to delete leftovers)
        self.last_error = ""
        self.last_saved = 0.0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token and "/" in self.repo)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=20, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "persona-telegram-bot"})

    def _url(self, name: str = "") -> str:
        return f"{self.api}/repos/{self.repo}/actions/variables" + (f"/{name}" if name else "")

    # ---------- reading ----------
    def _list(self, http: httpx.Client) -> dict[str, str]:
        out, page = {}, 1
        while page < 40:
            r = http.get(self._url(), params={"per_page": 30, "page": page})
            r.raise_for_status()
            items = r.json().get("variables") or []
            for v in items:
                if str(v.get("name", "")).startswith(self.prefix + "_"):
                    out[v["name"]] = v.get("value", "")
            if len(items) < 30:
                break
            page += 1
        return out

    def fetch(self) -> dict[str, tuple[float, str]]:
        """{filename: (saved_at, content)} from the stored copy."""
        if not self.enabled:
            return {}
        with self._client() as http:
            raw = self._list(http)
        found = {}
        for filename in MIRRORED:
            slug = _slug(filename)
            first = raw.get(f"{self.prefix}_{slug}_0")
            if not first or not (m := HEADER.match(first)):
                continue
            n, ts = int(m.group(1)), float(m.group(2))
            parts = [first[m.end():]] + [raw.get(f"{self.prefix}_{slug}_{i}") for i in range(1, n)]
            if any(p is None for p in parts):
                log.warning("stored %s is incomplete — skipped", filename)
                continue
            text = vault.decrypt("".join(parts))
            if text is None:
                log.warning("stored %s can't be decrypted (PERSONA_KEY changed?) — skipped", filename)
                continue
            found[filename] = (ts, text)
            self.chunks[slug] = max(n, sum(1 for k in raw if k.startswith(f"{self.prefix}_{slug}_")))
        return found

    def restore(self, data_dir: Path) -> list[str]:
        """Brings back every stored file that is missing locally or older than the stored copy."""
        restored = []
        try:
            stored = self.fetch()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("couldn't read the stored state: %s", self.last_error)
            return restored
        data_dir.mkdir(parents=True, exist_ok=True)
        for filename, (ts, text) in stored.items():
            path = data_dir / filename
            local_ts = path.stat().st_mtime if path.exists() else 0.0
            if ts > local_ts + 1:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(path)
                os.utime(path, (ts, ts))
                restored.append(filename)
        if restored:
            log.info("restored from the durable copy: %s", ", ".join(restored))
        return restored

    # ---------- writing ----------
    def schedule(self, filename: str, text: str) -> None:
        """Called on every write of a data file; mirrored ones are uploaded a moment later."""
        if not self.enabled or filename not in MIRRORED:
            return
        if len(text) > MIRRORED[filename]:
            log.warning("%s is too big to mirror (%d chars) — kept in the cache only", filename, len(text))
            return
        with self._lock:
            self.pending[filename] = text
            if self._timer is None:
                self._timer = threading.Timer(self.delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            pending, self.pending = self.pending, {}
            self._timer = None
        if not pending:
            return
        try:
            with self._client() as http:
                for filename, text in pending.items():
                    self._save(http, filename, text)
            self.last_error = ""
            self.last_saved = time.time()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("couldn't store the durable copy: %s", self.last_error)
            with self._lock:  # try again with the next change
                for k, v in pending.items():
                    self.pending.setdefault(k, v)

    def _save(self, http: httpx.Client, filename: str, text: str) -> None:
        slug = _slug(filename)
        blob = vault.encrypt(text)
        parts = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)] or [""]
        parts[0] = f"v1:{len(parts)}:{time.time():.3f}:" + parts[0]
        # later chunks first, the first one (with the header) last — a reader never sees a half-written set
        for i in reversed(range(len(parts))):
            name = f"{self.prefix}_{slug}_{i}"
            r = http.patch(self._url(name), json={"name": name, "value": parts[i]})
            if r.status_code == 404:
                r = http.post(self._url(), json={"name": name, "value": parts[i]})
            r.raise_for_status()
        for i in range(len(parts), self.chunks.get(slug, 0)):
            http.delete(self._url(f"{self.prefix}_{slug}_{i}"))
        self.chunks[slug] = len(parts)

    def status(self) -> str:
        if not self.enabled:
            return "off (needs GH_PAT with the repo scope)"
        if self.last_error:
            return f"error: {self.last_error}"
        return "on" + (f" · last saved {time.strftime('%H:%M:%S', time.localtime(self.last_saved))}"
                       if self.last_saved else "")

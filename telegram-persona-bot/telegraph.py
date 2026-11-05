"""عميل Telegra.ph: ينشئ صفحة مرتبة للردود الطويلة تنفتح داخل تليجرام بـ Instant View.

بلا حد حروف وبلا تقسيم الرد لأجزاء. الـ access_token ينحفظ عشان الصفحات تبقى تحت حساب واحد.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("persona-bot.telegraph")

API_BASE = os.getenv("TELEGRAPH_API_BASE", "https://api.telegra.ph").rstrip("/")
CONTENT_LIMIT = 63_000  # حد حقل المحتوى في Telegra.ph تقريباً 64KB


class TelegraphError(RuntimeError):
    pass


class Telegraph:
    def __init__(self, token_file: Path, short_name: str = "bot", author_name: str = ""):
        self.token_file = token_file
        self.short_name = (short_name or "bot")[:32]
        self.author_name = (author_name or "")[:128]
        self._token: str | None = None
        self._lock = asyncio.Lock()

    def _load_token(self) -> str | None:
        if self._token:
            return self._token
        try:
            self._token = json.loads(self.token_file.read_text(encoding="utf-8"))["access_token"]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            self._token = None
        return self._token

    def _save_token(self, token: str) -> None:
        self.token_file.parent.mkdir(exist_ok=True)
        self.token_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")

    async def _post(self, http: httpx.AsyncClient, method: str, data: dict) -> dict:
        r = await http.post(f"{API_BASE}/{method}", data=data)
        r.raise_for_status()
        j = r.json()
        if not j.get("ok"):
            raise TelegraphError(j.get("error", "unknown"))
        return j["result"]

    async def _ensure_token(self, http: httpx.AsyncClient) -> str:
        if self._load_token():
            return self._token
        async with self._lock:
            if self._load_token():
                return self._token
            res = await self._post(http, "createAccount",
                                   {"short_name": self.short_name, "author_name": self.author_name})
            self._token = res["access_token"]
            self._save_token(self._token)
            log.info("telegraph account created")
            return self._token

    async def create_page(self, title: str, nodes: list) -> str:
        """ينشئ صفحة ويرجع رابطها. يرمي TelegraphError لو فشل."""
        content = json.dumps(nodes, ensure_ascii=False)
        if len(content.encode("utf-8")) > CONTENT_LIMIT:
            raise TelegraphError("too_long")
        async with httpx.AsyncClient(timeout=25) as http:
            token = await self._ensure_token(http)
            data = {"access_token": token, "title": (title or "رد")[:256],
                    "content": content, "return_content": "false"}
            if self.author_name:
                data["author_name"] = self.author_name
            try:
                res = await self._post(http, "createPage", data)
            except TelegraphError as e:
                if "TOKEN" in str(e).upper():  # توكن قديم/غير صالح: ننشئ حساب جديد مرة وحدة
                    self._token = None
                    try:
                        self.token_file.unlink()
                    except FileNotFoundError:
                        pass
                    token = await self._ensure_token(http)
                    data["access_token"] = token
                    res = await self._post(http, "createPage", data)
                else:
                    raise
            return res["url"]

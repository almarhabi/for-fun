"""بوت تليجرام بهوية ثابتة يحددها المالك، يعمل بـ Kimi K3.1 Pro، ويتصرف مثل تطبيقات الذكاء الاصطناعي."""

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, AsyncOpenAI
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import github_tools
import render
import telegraph
from render import RLM, esc
from tools import TOOLS, describe_call, run_tool

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
def clean_key(key: str) -> str:
    """ينظف المفتاح من الأخطاء الشائعة وقت النسخ: مسافات، علامات تنصيص، كلمة Bearer."""
    key = key.strip().strip("\"'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


OPENROUTER_API_KEY = clean_key(os.environ["OPENROUTER_API_KEY"])
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# الموديل والمزودين من config.json (مع إمكانية تجاوزهم بمتغيرات البيئة)
DEFAULT_MODEL = config.DEFAULT_MODEL
PROVIDERS = config.PROVIDERS
DEFAULT_PROVIDER = config.DEFAULT_PROVIDER
OWNER_ID = int(os.getenv("OWNER_ID", "").strip() or "0")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))  # 10 رسائل لكل طرف

DATA_DIR = Path(__file__).parent / "data"
PERSONA_FILE = DATA_DIR / "persona.json"
RULES_FILE = DATA_DIR / "rules.json"       # تعديل قواعد القفل من تليجرام
SNIPPETS_FILE = DATA_DIR / "snippets.json"  # اختصارات المالك (جمل/ملفات محفوظة)
HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCREEN_FILE = DATA_DIR / "screen.json"
TELEGRAPH_FILE = DATA_DIR / "telegraph.json"

MAX_TOOL_ROUNDS = 10     # يكفي لمهام GitHub (قراءة، تعديل، عدة ملفات، PR)
EDIT_INTERVAL = 1.0      # تليجرام يسمح بتعديل الرسالة تقريباً مرة بالثانية
GROUP_WAIT = 1.2         # انتظار بقية الأجزاء (ألبوم صور أو رسالة طويلة انقسمت)
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_CHARS = 60_000
SINGLE_MAX = 3900        # لو الرد أطول من كذا (حروف HTML) ننشره كصفحة Telegra.ph مرتبة

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("persona-bot")
for noisy in ("httpx", "primp", "ddgs"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL,
                     default_headers={"X-Title": "Persona Telegram Bot"})
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
PREVIEW = LinkPreviewOptions(is_disabled=False, prefer_large_media=False)
tph = telegraph.Telegraph(TELEGRAPH_FILE, short_name="PersonaBot")


# ================= التخزين =================

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ذاكرة المحادثة لكل شات
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
# أرقام الرسائل الظاهرة في كل شات (عشان "محادثة جديدة" تمسح الشاشة)
screen: dict[int, list[int]] = defaultdict(list)


def load_state() -> None:
    for chat_id, msgs in _read_json(HISTORY_FILE, {}).items():
        histories[int(chat_id)].extend(msgs)
    for chat_id, ids in _read_json(SCREEN_FILE, {}).items():
        screen[int(chat_id)] = ids[-1000:]


def save_histories() -> None:
    _write_json(HISTORY_FILE, {str(k): list(v) for k, v in histories.items() if v})


def track(chat_id: int, *message_ids: int | None) -> None:
    ids = screen[chat_id]
    ids.extend(m for m in message_ids if m)
    del ids[:-1000]
    _write_json(SCREEN_FILE, {str(k): v for k, v in screen.items() if v})


def untrack(chat_id: int, message_ids: list[int]) -> None:
    drop = set(message_ids)
    screen[chat_id] = [m for m in screen[chat_id] if m not in drop]
    _write_json(SCREEN_FILE, {str(k): v for k, v in screen.items() if v})


# ================= الهوية والقواعد =================

# قواعد الهوية والشكل وملاحظة GitHub — كلها في config.json (عدّلها هناك)
LOCK_RULES = config.LOCK_RULES
STYLE_RULES = config.STYLE_RULES
GITHUB_NOTE = config.GITHUB_NOTE
DEFAULT_PERSONA = config.DEFAULT_PERSONA


def load_persona() -> str:
    data = _read_json(PERSONA_FILE, {})
    return data["persona"] if isinstance(data, dict) and "persona" in data else DEFAULT_PERSONA


def save_persona(text: str) -> None:
    _write_json(PERSONA_FILE, {"persona": text})


def load_lock() -> str:
    """قواعد القفل الحالية — المعدّلة من تليجرام إن وُجدت، وإلا من config.json."""
    data = _read_json(RULES_FILE, {})
    return data["lock"] if isinstance(data, dict) and "lock" in data else config.LOCK_RULES


def save_lock(text: str) -> None:
    _write_json(RULES_FILE, {"lock": text})


def load_snippets() -> dict:
    data = _read_json(SNIPPETS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_snippets(data: dict) -> None:
    _write_json(SNIPPETS_FILE, data)


def system_prompt(github: bool = False) -> str:
    extra = GITHUB_NOTE if github else ""
    return f"{load_lock()}\n{STYLE_RULES}{extra}\n<identity>\n{load_persona()}\n</identity>"


# ================= الإعدادات =================

# مستويات التفكير والإعدادات وقيمها الافتراضية — كلها في config.json
EFFORTS = config.EFFORTS
EFFORT_API = config.EFFORT_API
PARAMS = config.PARAMS
BOOL_KEYS = config.BOOL_KEYS
DEFAULTS = config.DEFAULTS


def cfg(key: str):
    value = _read_json(SETTINGS_FILE, {}).get(key, DEFAULTS[key])
    if key == "model" and "/" not in str(value):  # اسم قديم من Moonshot مباشرة
        return DEFAULT_MODEL
    return value


def save_setting(key: str, value) -> None:
    data = _read_json(SETTINGS_FILE, {})
    data[key] = value
    _write_json(SETTINGS_FILE, data)


def parse_value(key: str, raw: str):
    typ = PARAMS[key][0]
    raw = raw.strip()
    low = raw.lower()
    if typ is bool:
        if low in ("on", "true", "1", "yes", "تشغيل"):
            return True
        if low in ("off", "false", "0", "no", "ايقاف", "إيقاف"):
            return False
        raise ValueError("اكتب on أو off")
    if key == "provider" and low in ("any", "all", "أي"):
        return "any"
    if low in ("off", "none", "default", "افتراضي"):
        return "default" if key == "effort" else DEFAULTS[key] if key in ("model", "provider") else None
    if key == "effort":
        if low not in EFFORTS:
            raise ValueError("القيم: " + " / ".join(EFFORTS))
        return low
    try:
        return typ(raw)
    except ValueError:
        raise ValueError(f"لازم تكون رقم ({typ.__name__})") from None


def request_kwargs(overrides: dict | None = None) -> dict:
    o = overrides or {}

    def get(k):
        return o.get(k, cfg(k))

    effort = get("effort")
    extra = {"reasoning": {"enabled": True} if effort == "default" else {"effort": EFFORT_API.get(effort, effort)}}
    provider = get("provider")
    if provider and provider != "any":
        extra["provider"] = {"only": [p.strip() for p in provider.split(",") if p.strip()], "allow_fallbacks": False}
    kw = {"model": get("model"), "extra_body": extra}
    for k in ("temperature", "top_p", "max_tokens"):
        if get(k) is not None:
            kw[k] = get(k)
    return kw


def split_overrides(text: str) -> tuple[dict, str]:
    """إعدادات لرسالة وحدة في بدايتها، مثل: effort=low temperature=0.3 السؤال"""
    overrides, words = {}, text.split(" ")
    while words and "=" in words[0]:
        k, _, v = words[0].partition("=")
        if k not in PARAMS:
            break
        overrides[k] = parse_value(k, v)
        words.pop(0)
    return overrides, " ".join(words).strip()


# ================= أدوات مساعدة لتليجرام =================

NEW_CHAT_BTN = config.NEW_CHAT_BTN
SETTINGS_BTN = config.SETTINGS_BTN
STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="stop")]])
REGEN_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 إعادة التوليد", callback_data="regen")]])


def is_owner_id(user_id: int | None) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and is_owner_id(update.effective_user.id)


def main_keyboard(owner: bool) -> ReplyKeyboardMarkup:
    row = [NEW_CHAT_BTN, SETTINGS_BTN] if owner else [NEW_CHAT_BTN]
    return ReplyKeyboardMarkup([row], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="اكتب رسالتك…")


def lines_html(*lines: str) -> str:
    """يرتب أسطر رسائل البوت (القوائم والتنبيهات) من اليمين لليسار."""
    return "\n".join(RLM + ln if ln.strip() else ln for ln in lines)


async def send_html(bot, chat_id: int, text: str, markup=None, keep: bool = True) -> Message | None:
    """يرسل رسالة HTML، ولو فشل التنسيق يرسلها نص عادي. keep=True يسجلها عشان تنمسح مع المحادثة الجديدة."""
    try:
        try:
            msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                         link_preview_options=NO_PREVIEW)
        except BadRequest as e:
            if "parse" not in str(e).lower():
                raise
            msg = await bot.send_message(chat_id, render.to_plain(text), reply_markup=markup,
                                         link_preview_options=NO_PREVIEW)
    except TelegramError as e:
        log.warning("send failed: %s", e)
        return None
    if keep:
        track(chat_id, msg.message_id)
    return msg


async def delete_many(bot, chat_id: int, ids: list[int]) -> None:
    ids = sorted(set(ids))
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        try:
            await bot.delete_messages(chat_id, batch)
        except TelegramError:
            for mid in batch[-30:]:  # لو الدفعة فشلت نحاول آخر الرسائل وحدة وحدة
                with contextlib.suppress(TelegramError):
                    await bot.delete_message(chat_id, mid)


def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """في الخاص يرد على كل شي، وفي القروبات بس لو أحد منشنه أو رد عليه."""
    msg = update.message
    if not msg or update.effective_chat.type == ChatType.PRIVATE:
        return True
    username = (context.bot.username or "").lower()
    text = (msg.text or msg.caption or "").lower()
    replied = msg.reply_to_message and msg.reply_to_message.from_user and \
        msg.reply_to_message.from_user.id == context.bot.id
    return bool(replied or (username and f"@{username}" in text))


# ================= عرض الرد المباشر =================

class Panel:
    """مجموعة رسائل تليجرام تتحدث (رسالة التفكير، أو رسائل الرد)."""

    def __init__(self, bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id
        self.msgs: list[Message] = []
        self.shown: list[tuple] = []

    async def _do(self, final: bool, fn, **kwargs):
        for attempt in range(4):
            try:
                return await fn(**kwargs)
            except RetryAfter as e:
                if not final or attempt == 3:
                    raise
                await asyncio.sleep(e.retry_after + 0.5)
            except BadRequest as e:
                err = str(e).lower()
                if "not modified" in err:
                    return True
                if "parse" in err and kwargs.get("parse_mode"):
                    # لو تليجرام رفض التنسيق نرسلها نص عادي بدل ما يضيع الرد
                    kwargs["parse_mode"] = None
                    kwargs["text"] = render.to_plain(kwargs["text"])
                    continue
                if "not found" in err or "can't be edited" in err:
                    return True
                log.warning("telegram error: %s", e)
                return None
        return None

    async def sync(self, pages: list, final: bool) -> None:
        pages = [(p[0], p[1], p[2] if len(p) > 2 else False) for p in pages]
        for i, (text, markup, preview) in enumerate(pages):
            lpo = PREVIEW if preview else NO_PREVIEW
            key = (text, markup.to_json() if markup else None, preview)
            if i < len(self.msgs):
                if self.shown[i] == key:
                    continue
                ok = await self._do(final, self.msgs[i].edit_text, text=text, parse_mode=ParseMode.HTML,
                                    reply_markup=markup, link_preview_options=lpo)
                if ok:
                    self.shown[i] = key
            else:
                msg = await self._do(final, self.bot.send_message, chat_id=self.chat_id, text=text,
                                     parse_mode=ParseMode.HTML, reply_markup=markup,
                                     link_preview_options=lpo)
                if isinstance(msg, Message):
                    self.msgs.append(msg)
                    self.shown.append(key)
                else:
                    return
        while len(self.msgs) > len(pages):
            msg = self.msgs.pop()
            self.shown.pop()
            with contextlib.suppress(TelegramError):
                await msg.delete()

    def ids(self) -> list[int]:
        return [m.message_id for m in self.msgs]


think_store: "OrderedDict[str, str]" = OrderedDict()  # التفكير الكامل لزر "📄 التفكير كامل"


def remember_thinking(text: str) -> str:
    key = str(int(time.time() * 1000))[-9:]
    think_store[key] = text
    while len(think_store) > 100:
        think_store.popitem(last=False)
    return key


class ResponseUI:
    """يعرض رد واحد: رسالة تفكير تتحدث أولاً، ثم رسالة الرد تنكتب قدام المستخدم."""

    def __init__(self, bot, chat_id: int, show_thinking: bool, long_pages: bool = True):
        self.bot, self.chat_id, self.show_thinking = bot, chat_id, show_thinking
        self.long_pages = long_pages
        self.think = Panel(bot, chat_id)
        self.reply = Panel(bot, chat_id)
        self.parts: list[list[str]] = []      # [["r", تفكير] أو ["s", خطوة بحث]]
        self._round_part: list[str] | None = None
        self.reply_md = ""
        self._reply_base = ""
        self.status = ""
        self.sources: list[dict] = []
        self.started = self.last_think = time.monotonic()
        self.stopped = self.finishing = False
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._cooldown = 0.0
        self._think_key: str | None = None

    # ----- تحديث الحالة (بدون انتظار) -----
    def begin_round(self) -> None:
        self._round_part = None
        self._reply_base = self.reply_md

    def set_round_reasoning(self, text: str) -> None:
        if not text.strip():
            return
        if self._round_part is None:
            self._round_part = ["r", ""]
            self.parts.append(self._round_part)
        self._round_part[1] = text
        self.last_think = time.monotonic()

    def set_round_answer(self, answer: str) -> None:
        # الرد يتجمع عبر الجولات: النص اللي انكتب قبل البحث يبقى، والجديد ينضاف بعده
        base = self._reply_base
        sep = "\n\n" if base.strip() and answer and not base.endswith("\n") else ""
        self.reply_md = base + sep + answer
        if answer:
            self.status = ""

    def add_step(self, text: str) -> None:
        self.parts.append(["s", text])
        self._round_part = None
        self.status = text + "…"
        self.last_think = time.monotonic()

    def add_sources(self, items: list[dict]) -> None:
        known = {s["url"] for s in self.sources}
        for s in items:
            if s.get("url") and s["url"] not in known:
                self.sources.append(s)
                known.add(s["url"])

    def stop(self) -> bool:
        if self.finishing or self.stopped or not self.task:
            return False
        self.stopped = True
        self.task.cancel()
        return True

    # ----- الرسم -----
    def _reply_active(self) -> bool:
        return bool(self.reply_md.strip()) or not self.show_thinking

    def think_pages(self, final: bool) -> list:
        if not self.show_thinking or (final and not self.parts):
            return []
        text, truncated = render.thinking_html(self.parts, final, self.last_think - self.started)
        markup = None
        if not final and not self._reply_active():
            markup = STOP_KB
        elif final and truncated:
            if not self._think_key:
                self._think_key = remember_thinking(self._full_thinking())
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("📄 التفكير كامل", callback_data=f"think:{self._think_key}")]])
        return [(text, markup)]

    def _full_thinking(self) -> str:
        return "\n\n".join(t for _, t in self.parts)

    def _final_extras(self) -> list[str]:
        extras = []
        if self.stopped:
            extras.append(RLM + "<i>⏹ تم إيقاف الرد</i>")
        if self.error:
            extras.append(self.error)
        return extras

    def live_pages(self) -> list:
        """معاينة حيّة: رسالة وحدة تتحدث وهو يكتب. ما نقسّم ولا ننشر صفحة إلا عند النهاية."""
        if not self._reply_active():
            return []
        h = render.live_tail_html(self.reply_md) if self.reply_md.strip() else ""
        if not h and self.status:
            h = RLM + f"<i>{esc(self.status)}</i>"
        h = f"{h} {render.CURSOR}" if h else render.CURSOR
        return [(h, STOP_KB, False)]

    async def final_reply_pages(self) -> list:
        """تسليم الرد النهائي: رسالة وحدة لو قصير، صفحة Telegra.ph لو طويل، وتقسيم احتياطي."""
        md = self.reply_md.strip("\n")
        extras = self._final_extras()
        src_html = render.sources_html(self.sources) if self.sources else ""
        if not md:
            body = "\n\n".join(extras) if extras else RLM + "<i>…</i>"
            return [(body, REGEN_KB, False)]
        full = render.render(md, partial=False)  # كامل بلا قص؛ القرار على الطول الحقيقي
        tail = ("\n\n" + "\n\n".join(extras)) if extras else ""
        if len(full) + len(src_html) + len(tail) <= SINGLE_MAX:  # يكفي رسالة وحدة
            body = full + (("\n\n" + src_html) if src_html else "") + tail
            return [(body, REGEN_KB, False)]
        if self.long_pages and not self.stopped:  # رد طويل → صفحة تنفتح داخل تليجرام
            try:
                nodes = render.to_telegraph_nodes(md)
                if self.sources:
                    nodes.append({"tag": "hr"})
                    nodes.append({"tag": "h3", "children": ["المصادر"]})
                    nodes.append({"tag": "ul", "children": [
                        {"tag": "li", "children": [{"tag": "a", "attrs": {"href": s["url"]},
                                                    "children": [s.get("title") or s["url"]]}]}
                        for s in self.sources if s.get("url")]})
                url = await tph.create_page(render.telegraph_title(md), nodes)
                head = lines_html("📄 <b>الرد كامل</b> — طويل، فخليته بصفحة مرتبة تنفتح جوّا تليجرام:",
                                  f'<a href="{esc(url)}">📖 افتح الرد كامل</a>')
                return [(head + tail, REGEN_KB, True)]
            except Exception:
                log.warning("telegraph failed; splitting instead", exc_info=True)
        chunks = render.split_md(md)  # احتياطي: تقسيم — نضمن إن ما ينقص شي
        pages = [[render.page_html(c, False), None, False] for c in chunks]
        if src_html:
            pages.append([src_html, None, False])
        if extras:
            pages[-1][0] += "\n\n" + "\n\n".join(extras)
        pages[-1][1] = REGEN_KB
        return [tuple(p) for p in pages]

    async def flush(self) -> None:
        async with self._lock:
            if time.monotonic() < self._cooldown:
                return
            try:
                await self.think.sync(self.think_pages(False), False)
                await self.reply.sync(self.live_pages(), False)
            except RetryAfter as e:
                self._cooldown = time.monotonic() + e.retry_after
            except TelegramError as e:
                log.warning("flush failed: %s", e)

    async def _loop(self) -> None:
        last_typing = 0.0
        while True:
            await asyncio.sleep(EDIT_INTERVAL)
            if time.monotonic() - last_typing > 4.5:
                last_typing = time.monotonic()
                with contextlib.suppress(TelegramError):
                    await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
            await self.flush()

    async def start(self) -> None:
        await self.flush()  # يطلع "يفكر…" فوراً
        self._loop_task = asyncio.create_task(self._loop())

    async def finish(self) -> None:
        self.finishing = True
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        async with self._lock:
            with contextlib.suppress(TelegramError):
                await self.think.sync(self.think_pages(True), True)
            pages = await self.final_reply_pages()
            with contextlib.suppress(TelegramError):
                await self.reply.sync(pages, True)

    def message_ids(self) -> list[int]:
        return self.think.ids() + self.reply.ids()


# ================= التوليد =================

@dataclass
class Turn:
    """رسالة المستخدم ورد البوت عليها (عشان إعادة التوليد وتعديل الرسالة)."""
    user_msg_id: int | None
    content: object          # اللي ينرسل للموديل (نص، أو نص + صور)
    history_text: str        # اللي ينحفظ في الذاكرة
    overrides: dict
    user_id: int | None
    response_ids: list[int] = field(default_factory=list)


last_turn: dict[int, Turn] = {}
no_tools: set[str] = set()  # موديل/مزود ما يدعم الأدوات


def tools_key(kw: dict) -> str:
    return kw["model"] + "|" + json.dumps(kw.get("extra_body", {}).get("provider"), sort_keys=True)


def keep_recent_reasoning(history: deque, keep: int = 10) -> None:
    """نخلي reasoning_details في آخر 10 ردود (كل ردود الذاكرة)."""
    seen = 0
    for msg in reversed(history):
        if msg.get("role") == "assistant" and "reasoning_details" in msg:
            seen += 1
            if seen > keep:
                msg.pop("reasoning_details")
active: dict[int, ResponseUI] = {}
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def reasoning_delta(delta) -> tuple[str, list]:
    """OpenRouter يرسل التفكير كنص (reasoning) وكتفاصيل كاملة (reasoning_details)."""
    extra = getattr(delta, "model_extra", None) or {}
    details = extra.get("reasoning_details")
    details = details if isinstance(details, list) else []
    text = extra.get("reasoning") or extra.get("reasoning_content") or getattr(delta, "reasoning_content", None)
    if not text and details:
        text = "".join((d.get("text") or d.get("summary") or "") for d in details if isinstance(d, dict))
    return (text if isinstance(text, str) else ""), details


def merge_reasoning_details(acc: list, new: list) -> None:
    """يجمع أجزاء reasoning_details من البث عشان نرجعها للموديل مثل ما هي."""
    for d in new:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        target = next((x for x in acc if idx is not None and x.get("index") == idx
                       and x.get("type") == d.get("type")), None)
        if target is None:
            acc.append(dict(d))
            continue
        for k, v in d.items():
            if k in ("text", "summary", "data") and isinstance(v, str):
                target[k] = (target.get(k) or "") + v
            elif v is not None:
                target[k] = v


def split_think_tags(raw: str) -> tuple[str, str]:
    """بعض الموديلات تكتب التفكير داخل <think>...</think> في نفس الرد."""
    s = raw.lstrip()
    if not s.startswith("<think>"):
        return "", raw
    end = s.find("</think>")
    if end == -1:
        return s[7:], ""
    return s[7:end], s[end + 8 :].lstrip()


async def stream_round(ui: ResponseUI, messages: list, kw: dict, tool_kwargs: dict):
    """جولة وحدة مع الموديل. يرجع (نص الرد، التفكير، تفاصيل التفكير، طلبات الأدوات)."""
    stream = await client.chat.completions.create(messages=messages, stream=True, **kw, **tool_kwargs)
    raw, reasoning, details, calls = "", "", [], {}
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            r_text, r_details = reasoning_delta(delta)
            reasoning += r_text
            merge_reasoning_details(details, r_details)
            raw += delta.content or ""
            for tc in delta.tool_calls or []:
                acc = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                acc["id"] = tc.id or acc["id"]
                if tc.function:
                    acc["name"] += tc.function.name or ""
                    acc["args"] += tc.function.arguments or ""
            tag_think, answer = split_think_tags(raw)
            ui.set_round_reasoning(reasoning + tag_think)
            ui.set_round_answer(answer)
    finally:
        with contextlib.suppress(Exception):
            await stream.close()
    _, answer = split_think_tags(raw)
    return answer, reasoning, details, [calls[i] for i in sorted(calls)]


def error_hint(e: Exception) -> str:
    code = getattr(e, "status_code", None)
    msg = str(e).lower()
    if code == 401:
        return ("OpenRouter رفض المفتاح. تأكد إن السر OPENROUTER_API_KEY فيه المفتاح كامل "
                "(يبدأ بـ sk-or-v1-) من openrouter.ai/keys، وإن المفتاح ما انحذف.")
    if code == 402 or "credit" in msg:
        return "الرصيد في OpenRouter ما يكفي."
    if code == 429:
        return "فيه ضغط على المزود، جرّب بعد شوي."
    if code == 404 or "no endpoints" in msg or "provider" in msg:
        return "المزود المحدد مو متاح حالياً — جرّب من ⚙️ الإعدادات: «أي مزود»."
    return ""


def error_html(e: Exception, owner: bool) -> str:
    text = RLM + "⚠️ <b>صار خطأ وما قدرت أكمل الرد.</b>\n" + RLM + "اضغط 🔄 عشان تحاول مرة ثانية."
    if owner and (hint := error_hint(e)):
        text += "\n" + RLM + "💡 " + esc(hint)
    if owner:
        text += f"\n<blockquote expandable>{esc(type(e).__name__)}: {esc(str(e)[:700])}</blockquote>"
    return text


regen_msg: dict[int, int] = {}  # الرسالة اللي عليها زر 🔄 في كل شات


async def drop_old_buttons(bot, chat_id: int) -> None:
    """زر إعادة التوليد يبقى على آخر رد بس."""
    mid = regen_msg.pop(chat_id, None)
    if mid:
        with contextlib.suppress(TelegramError):
            await bot.edit_message_reply_markup(chat_id, mid, reply_markup=None)


def active_tools(owner: bool, use_search: bool) -> list:
    """أدوات البحث للجميع، وأدوات GitHub للمالك فقط (ولو التوكن مضبوط)."""
    tools = list(TOOLS) if use_search else []
    if owner and github_tools.enabled():
        tools += github_tools.GITHUB_TOOLS
    return tools


def describe_tool(name: str, args: str) -> str:
    if github_tools.is_github_tool(name):
        return github_tools.describe_github(name, args)
    return describe_call(name, args)


async def dispatch_tool(name: str, args: str, owner: bool):
    """ينفّذ الأداة. أدوات GitHub للمالك فقط — فحص ثاني حتى لو انطلبت بالغلط."""
    if github_tools.is_github_tool(name):
        if not owner:
            return "هذي الأداة لصاحب البوت فقط.", []
        return await github_tools.run_github(name, args), []
    return await run_tool(name, args)


async def _generate(bot, chat_id: int, turn: Turn) -> None:
    """يولد رد كامل (لازم يكون قفل الشات ماسك)."""
    overrides = turn.overrides
    owner = is_owner_id(turn.user_id)
    github_on = owner and github_tools.enabled()
    show_thinking = overrides.get("show_thinking", cfg("show_thinking"))
    long_pages = overrides.get("long_pages", cfg("long_pages"))
    kw = request_kwargs(overrides)
    use_search = overrides.get("web_search", cfg("web_search")) and tools_key(kw) not in no_tools
    tools_list = active_tools(owner, use_search)
    rounds, final_details = 0, []
    await drop_old_buttons(bot, chat_id)

    history = histories[chat_id]
    messages = [{"role": "system", "content": system_prompt(github_on)}, *history,
                {"role": "user", "content": turn.content}]
    ui = ResponseUI(bot, chat_id, show_thinking, long_pages)
    ui.task = asyncio.current_task()
    active[chat_id] = ui
    try:
        await ui.start()
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            if not tools_list:
                tool_kwargs = {}
            elif round_no < MAX_TOOL_ROUNDS:
                tool_kwargs = {"tools": tools_list}
            else:  # خلصت الجولات — لازم يجاوب بدون أدوات إضافية
                tool_kwargs = {"tools": tools_list, "tool_choice": "none"}
            ui.begin_round()
            try:
                content, reasoning, details, calls = await stream_round(ui, messages, kw, tool_kwargs)
            except APIError as e:
                # بعض المزودين ما يدعمون الأدوات: نكمل بدونها بدل ما يفشل الرد
                if not tool_kwargs or "tool" not in str(e).lower():
                    raise
                no_tools.add(tools_key(kw))
                tools_list, tool_kwargs = [], {}
                ui.add_step("⚠️ الأدوات غير مدعومة مع هذا المزود — كمّلت بدونها")
                ui.begin_round()
                content, reasoning, details, calls = await stream_round(ui, messages, kw, tool_kwargs)
            rounds += 1
            final_details = details
            if not calls:
                break
            assistant = {
                "role": "assistant",
                "content": content,
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["args"]}} for c in calls],
            }
            if details:
                assistant["reasoning_details"] = details  # نرجعها مثل ما هي عشان يكمل تفكيره
            messages.append(assistant)
            for c in calls:
                ui.add_step(describe_tool(c["name"], c["args"]))
                result, sources = await dispatch_tool(c["name"], c["args"], owner)
                ui.add_sources(sources)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    except asyncio.CancelledError:
        if not ui.stopped:
            raise
        asyncio.current_task().uncancel()  # المستخدم ضغط ⏹ — نكمل ونثبت اللي انكتب
    except Exception as e:
        log.exception("model API error")
        ui.error = error_html(e, is_owner_id(turn.user_id))
    finally:
        ui.finishing = True
        if active.get(chat_id) is ui:
            active.pop(chat_id)

    await ui.finish()
    turn.response_ids = ui.message_ids()
    last_turn[chat_id] = turn
    if turn.response_ids:
        regen_msg[chat_id] = turn.response_ids[-1]
    track(chat_id, *turn.response_ids)
    reply = ui.reply_md.strip()
    if reply and not ui.error:
        history.append({"role": "user", "content": turn.history_text})
        entry = {"role": "assistant", "content": reply}
        if final_details and rounds == 1 and not ui.stopped:
            entry["reasoning_details"] = final_details  # يكمل تفكيره من حيث وقف في الرسالة الجاية
        history.append(entry)
        keep_recent_reasoning(history)
        save_histories()


async def generate(bot, chat_id: int, turn: Turn) -> None:
    async with chat_locks[chat_id]:  # الرسائل الجديدة تنتظر دورها
        await _generate(bot, chat_id, turn)


async def redo(bot, chat_id: int, user_msg_id: int | None, replacement: Turn | None = None) -> bool:
    """يعيد آخر رد (🔄) أو يرد من جديد على رسالة انعدلت."""
    ui = active.get(chat_id)
    if ui:
        ui.stop()
    async with chat_locks[chat_id]:
        turn = last_turn.get(chat_id)
        if not turn or turn.user_msg_id != user_msg_id:
            return False
        history = histories[chat_id]
        if len(history) >= 2 and history[-2].get("content") == turn.history_text and history[-1]["role"] == "assistant":
            history.pop()
            history.pop()
            save_histories()
        await delete_many(bot, chat_id, turn.response_ids)
        untrack(chat_id, turn.response_ids)
        last_turn.pop(chat_id, None)
        await _generate(bot, chat_id, replacement or Turn(turn.user_msg_id, turn.content, turn.history_text,
                                                          turn.overrides, turn.user_id))
        return True


# ================= استقبال رسائل المستخدم =================

TEXT_EXT = {"txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "xml", "yaml", "yml", "ini", "toml", "cfg",
            "log", "py", "js", "ts", "tsx", "jsx", "html", "htm", "css", "scss", "java", "kt", "c", "h", "cpp",
            "hpp", "cs", "go", "rs", "rb", "php", "sh", "bash", "sql", "swift", "dart", "lua", "r", "m", "tex", "srt"}


def decode_text(data: bytes) -> str | None:
    for enc in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        return text
    return None


@dataclass
class Item:
    msg: Message
    text: str = ""
    images: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)


async def download(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


async def build_item(msg: Message, bot) -> Item | str:
    """يحول رسالة المستخدم لمحتوى. يرجع نص خطأ لو النوع ما ينفع."""
    item = Item(msg=msg, text=msg.text or msg.caption or "")
    if msg.photo:
        data = await download(bot, msg.photo[-1].file_id)
        item.images.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
    elif msg.document:
        doc = msg.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            return "الملف كبير، الحد 10 ميقا."
        name = doc.file_name or "file"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = doc.mime_type or ""
        data = await download(bot, doc.file_id)
        if mime.startswith("image/"):
            item.images.append(f"data:{mime};base64," + base64.b64encode(data).decode())
        elif ext == "pdf" or mime == "application/pdf":
            text = await asyncio.to_thread(pdf_text, data)
            if not text.strip():
                return "ملف الـ PDF ما فيه نص أقدر أقراه (غالباً صور)."
            item.files.append((name, text))
        else:
            text = decode_text(data) if (ext in TEXT_EXT or mime.startswith("text/") or not ext) else None
            if text is None:
                return f"ما أقدر أقرأ ملفات .{ext or '؟'} — أرسل نص أو صورة أو PDF أو ملف نصي."
            item.files.append((name, text))
    elif not item.text:
        return "ما أقدر أتعامل مع هذا النوع من الرسائل. أرسل نص أو صورة أو ملف."
    return item


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages[:80])


def quote_context(msg: Message) -> str:
    """لو المستخدم رد على رسالة، نعطي الموديل المقطع اللي رد عليه."""
    replied = msg.reply_to_message
    if not replied:
        return ""
    quoted = (msg.quote.text if getattr(msg, "quote", None) else None) or replied.text or replied.caption or ""
    quoted = quoted.strip()
    if not quoted:
        return ""
    if len(quoted) > 1500:
        quoted = quoted[:1500] + "…"
    return f"[المستخدم يرد على هذا المقطع: «{quoted}»]\n"


def build_turn(items: list[Item], owner: bool) -> Turn | str | None:
    text = "\n".join(it.text for it in items if it.text)
    overrides = {}
    if owner and text:
        try:
            overrides, text = split_overrides(text)
        except ValueError as e:
            return f"إعداد غلط في الرسالة: {e}"
    images = [u for it in items for u in it.images]
    files = [f for it in items for f in it.files]
    if not text and not images and not files:
        return None
    prefix = quote_context(items[0].msg)
    file_blocks = "".join(f"📎 الملف: {name}\n```\n{body[:MAX_FILE_CHARS]}\n```\n\n" for name, body in files)
    full = prefix + file_blocks + text
    content: object = full
    if images:
        content = [{"type": "text", "text": full or "وش تشوف في الصورة؟"}] + \
                  [{"type": "image_url", "image_url": {"url": u}} for u in images]
    history_text = prefix + "".join("[🖼 صورة] " for _ in images) + \
        "".join(f"📎 الملف: {n}\n```\n{b[:20_000]}\n```\n\n" for n, b in files) + text
    return Turn(user_msg_id=items[-1].msg.message_id, content=content, history_text=history_text.strip(),
                overrides=overrides, user_id=items[0].msg.from_user.id if items[0].msg.from_user else None)


pending: dict[int, dict] = {}  # رسائل تنتظر بقية أجزائها


async def process_items(bot, chat_id: int, items: list[Item]) -> None:
    owner = bool(items[0].msg.from_user) and is_owner_id(items[0].msg.from_user.id)
    turn = build_turn(items, owner)
    if turn is None:
        return
    if isinstance(turn, str):
        await send_html(bot, chat_id, RLM + "⚠️ " + esc(turn))
        return
    await generate(bot, chat_id, turn)


async def _flush_pending(bot, chat_id: int) -> None:
    await asyncio.sleep(GROUP_WAIT)
    entry = pending.pop(chat_id, None)
    if entry:
        try:
            await process_items(bot, chat_id, entry["items"])
        except Exception:
            log.exception("processing failed")


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not should_respond(update, context):
        return
    chat_id = msg.chat_id
    track(chat_id, msg.message_id)
    try:
        item = await build_item(msg, context.bot)
    except Exception:
        log.exception("download failed")
        item = "ما قدرت أحمّل الملف، جرّب مرة ثانية."
    if isinstance(item, str):
        await send_html(context.bot, chat_id, RLM + "⚠️ " + esc(item))
        return
    if username := context.bot.username:
        item.text = re.sub(rf"@{re.escape(username)}\b", "", item.text, flags=re.I).strip()
    if is_owner(update):  # نوسّع اختصارات المالك (!اسم) قبل الإرسال للموديل
        item.text = expand_snippets(item.text)

    # ألبوم صور أو رسالة طويلة انقسمت: ننتظر شوي ونجمعها في رسالة وحدة
    entry = pending.get(chat_id)
    looks_split = msg.media_group_id is not None or len(msg.text or "") >= 3500
    if entry or looks_split:
        if entry:
            entry["items"].append(item)
            entry["task"].cancel()
        else:
            entry = pending[chat_id] = {"items": [item]}
        entry["task"] = asyncio.create_task(_flush_pending(context.bot, chat_id))
        return
    await process_items(context.bot, chat_id, [item])


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """المستخدم عدّل رسالته الأخيرة: نرد عليها من جديد (مثل تطبيقات الذكاء الاصطناعي)."""
    msg = update.edited_message
    if not msg or not msg.text:
        return
    turn = last_turn.get(msg.chat_id)
    if not turn or turn.user_msg_id != msg.message_id:
        return
    item = Item(msg=msg, text=msg.text)
    new = build_turn([item], is_owner(update))
    if isinstance(new, Turn):
        await redo(context.bot, msg.chat_id, msg.message_id, replacement=new)


# ================= الأزرار =================

async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    ui = active.get(q.message.chat_id) if q.message else None
    if ui and q.message.message_id in ui.message_ids() and ui.stop():
        await q.answer("⏹ تم الإيقاف")
    else:
        await q.answer()


async def on_regen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = q.message.chat_id
    if chat_id in active:
        await q.answer("استنى يخلص الرد الحالي")
        return
    turn = last_turn.get(chat_id)
    if not turn or q.message.message_id not in turn.response_ids:
        await q.answer("أقدر أعيد توليد آخر رد بس")
        return
    await q.answer("🔄 جاري إعادة التوليد")
    await redo(context.bot, chat_id, turn.user_msg_id)


async def on_full_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    text = think_store.get(q.data.split(":", 1)[1])
    if not text:
        await q.answer("التفكير الكامل ما عاد محفوظ", show_alert=True)
        return
    await q.answer()
    msg = await context.bot.send_document(q.message.chat_id, InputFile(io.BytesIO(text.encode()), filename="التفكير.txt"),
                                          caption="💭 سلسلة التفكير كاملة")
    track(q.message.chat_id, msg.message_id)


# ================= الأوامر =================

def welcome_html(owner: bool) -> str:
    text = lines_html(
        "✨ <b>أهلاً فيك!</b>",
        "اكتب أي شي وأنا حاضر.",
        "",
        "<b>وش تقدر تسوي:</b>",
        "• تشوف تفكيري والرد وهو ينكتب قدامك",
        "• أبحث لك في الإنترنت 🔎 وأعطيك المصادر",
        "• ترسل لي صور 🖼 أو ملفات 📎 (PDF ونصوص)",
        "• توقف الرد ⏹ أو تعيد توليده 🔄",
        "• تعدّل رسالتك ✏️ وأرد عليها من جديد",
        "• ترد على أي رسالة عشان تسألني عنها",
        "",
        f"<b>{NEW_CHAT_BTN}</b> تحت أو /new — يبدأ من الصفر (الرسائل القديمة تبقى في الشات).",
    )
    if owner:
        gh = ("• 🐙 متصل بـ GitHub — أعطني أمر وأكتب/أعدّل/أنشئ مباشرة (مستودعات، ملفات، فروع، PR)"
              if github_tools.enabled() else
              "• 🐙 GitHub غير مربوط — أضف السر GH_PAT لأقدر أكتب وأعدّل على مستودعاتك")
        text += "\n\n" + lines_html(
            "👑 <b>أوامر المالك</b>",
            "• /setpersona — تحديد الهوية · /persona — عرضها",
            "• /setrules — تعديل قواعد القفل · /rules — عرضها",
            "• /snip <code>اسم نص</code> — حفظ اختصار، وبعدها اكتب <code>!اسم</code> بأي رسالة · /snippets",
            "• /settings — لوحة التحكم",
            "• /set <code>إعداد قيمة</code> — مثل <code>/set temperature 0.7</code>",
            "• /resetall — مسح ذاكرة كل المحادثات",
            "• إعدادات لرسالة وحدة: <code>effort=max web_search=off سؤالك</code>",
            gh,
        )
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    await send_html(context.bot, chat_id, welcome_html(is_owner(update)), main_keyboard(is_owner(update)))


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """محادثة جديدة: البوت ينسى كل شي صار قبل، والرسائل القديمة تبقى في الشات مثل ما هي."""
    chat_id = update.effective_chat.id
    ui = active.get(chat_id)
    if ui:
        ui.stop()
    pending.pop(chat_id, None)
    async with chat_locks[chat_id]:
        await drop_old_buttons(context.bot, chat_id)  # زر 🔄 القديم ما عاد يخص المحادثة الجديدة
        histories.pop(chat_id, None)
        save_histories()
        last_turn.pop(chat_id, None)
        await send_html(context.bot, chat_id, lines_html(
            "✨ <b>محادثة جديدة</b>",
            "بدأنا من الصفر — ما أتذكر أي شي من المحادثة اللي قبل.",
        ), main_keyboard(is_owner(update)))


async def owner_only(update: Update, context) -> bool:
    if is_owner(update):
        return True
    await send_html(context.bot, update.effective_chat.id, RLM + "🔒 هذا الأمر لصاحب البوت فقط.")
    return False


async def cmd_resetall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    histories.clear()
    save_histories()
    last_turn.clear()
    await send_html(context.bot, update.effective_chat.id, RLM + "🧹 <b>انمسحت ذاكرة كل المحادثات.</b>")


# ----- الهوية -----

capture: dict[int, dict] = {}  # وضع إدخال الهوية على عدة رسائل
PERSONA_KB = InlineKeyboardMarkup([[InlineKeyboardButton("✅ حفظ", callback_data="persona:save"),
                                    InlineKeyboardButton("✖️ إلغاء", callback_data="persona:cancel")]])


def after_command(text: str) -> str:
    """النص بعد الأمر بالضبط (نشيل بس المسافة اللي بعد الأمر)."""
    m = re.match(r"^/\S+", text)
    rest = text[m.end():] if m else text
    return rest[1:] if rest[:1] in (" ", "\n", "\t") else rest


async def read_persona_file(doc, bot) -> str:
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        raise ValueError("الملف كبير (الحد 2 ميقا)")
    text = decode_text(await download(bot, doc.file_id))
    if text is None:
        raise ValueError("ما قدرت أقرأ الملف — لازم يكون نص (txt) بترميز UTF-8")
    return text


# "الهدف" للتعديل: الهوية (persona) أو قواعد القفل (rules) — نفس آلية الإدخال لهما
TARGET_LABEL = {"persona": "الهوية", "rules": "قواعد القفل"}
TARGET_VIEW = {"persona": "/persona", "rules": "/rules"}


def _save_target(target: str, text: str) -> None:
    (save_persona if target == "persona" else save_lock)(text)


def load_target(target: str) -> str:
    return load_persona() if target == "persona" else load_lock()


async def store_control(bot, chat_id: int, text: str, target: str) -> None:
    _save_target(target, text)
    if target == "persona":  # الهوية الجديدة ما تتأثر بكلام قديم
        histories.clear()
        save_histories()
        last_turn.clear()
    label = TARGET_LABEL[target]
    lines = text.count("\n") + 1
    await send_html(bot, chat_id, lines_html(
        f"✅ <b>انحفظت {label} حرف بحرف</b>",
        f"📏 {len(text):,} حرف · {lines:,} سطر",
        f"تقدر تتأكد منها بـ {TARGET_VIEW[target]} (ترسل لك نسخة ملف طبق الأصل).",
    ))


async def start_capture(bot, chat_id: int, parts: list[str], target: str, note: str = "") -> None:
    prompt = await send_html(bot, chat_id, capture_text(parts, target, note), PERSONA_KB)
    capture[chat_id] = {"parts": parts, "prompt": prompt, "target": target}


def capture_text(parts: list[str], target: str, note: str = "") -> str:
    label = TARGET_LABEL[target]
    chars = sum(len(p) for p in parts)
    lines = [
        f"📝 <b>وضع إدخال {label}</b>",
        note or f"أرسل {label} الحين: رسالة وحدة، أو عدة رسائل، أو ملف <code>.txt</code>.",
        "كل حرف ينحفظ مثل ما هو بالضبط. للنصوص الطويلة جداً الأفضل ملف <code>.txt</code>.",
        "",
        f"📥 استلمت: <b>{len(parts)}</b> جزء · <b>{chars:,}</b> حرف",
        "لما تخلص اضغط ✅ حفظ (أو اكتب /done).",
    ]
    return lines_html(*lines)


async def set_control(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> None:
    if not await owner_only(update, context):
        return
    msg, bot, chat_id = update.message, context.bot, update.effective_chat.id
    track(chat_id, msg.message_id)
    raw = msg.text or msg.caption or ""
    rest = after_command(raw)
    replied = msg.reply_to_message
    doc = msg.document or (replied.document if replied else None)
    label = TARGET_LABEL[target]
    try:
        if doc:
            await store_control(bot, chat_id, await read_persona_file(doc, bot), target)
        elif rest.strip():
            if len(raw) >= 3900:  # تليجرام قسم الرسالة — ننتظر بقية الأجزاء
                await start_capture(bot, chat_id, [rest], target, f"{label} طويلة وتليجرام قسمها. أرسل بقية الأجزاء لو فيه.")
            else:
                await store_control(bot, chat_id, rest, target)
        elif replied and (replied.text or replied.caption):
            await store_control(bot, chat_id, replied.text or replied.caption, target)
        else:
            await start_capture(bot, chat_id, [], target)
    except ValueError as e:
        await send_html(bot, chat_id, RLM + "⚠️ " + esc(str(e)))


async def cmd_setpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "persona")


async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "rules")


async def finish_capture(bot, chat_id: int, save: bool) -> None:
    state = capture.pop(chat_id, None)
    if not state:
        return
    target = state.get("target", "persona")
    label = TARGET_LABEL[target]
    if state.get("prompt"):
        with contextlib.suppress(TelegramError):
            await state["prompt"].edit_reply_markup(None)
    if not save:
        await send_html(bot, chat_id, RLM + f"✖️ تم إلغاء تغيير {label}.")
    elif not state["parts"]:
        await send_html(bot, chat_id, RLM + f"⚠️ ما وصلني أي نص، {label} ما تغيّرت.")
    else:
        await store_control(bot, chat_id, "\n".join(state["parts"]), target)


async def capture_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يلتقط رسائل الهوية قبل أي شي ثاني (حتى لو بدأت بـ /)."""
    msg = update.message
    if not msg or msg.chat_id not in capture or not is_owner(update):
        return
    text = msg.text
    track(msg.chat_id, msg.message_id)
    if text in ("/done", "/save", "/تم"):
        await finish_capture(context.bot, msg.chat_id, True)
        raise ApplicationHandlerStop
    if text == "/cancel":
        await finish_capture(context.bot, msg.chat_id, False)
        raise ApplicationHandlerStop
    if text in (NEW_CHAT_BTN, SETTINGS_BTN) or (text or "").startswith(("/setpersona", "/setrules")):
        capture.pop(msg.chat_id, None)
        return
    state = capture[msg.chat_id]
    try:
        if msg.document:
            part = await read_persona_file(msg.document, context.bot)
        elif text is not None:
            part = text
        elif msg.caption:
            part = msg.caption
        else:
            await send_html(context.bot, msg.chat_id, RLM + "⚠️ أرسل نص أو ملف .txt")
            raise ApplicationHandlerStop
    except ValueError as e:
        await send_html(context.bot, msg.chat_id, RLM + "⚠️ " + esc(str(e)))
        raise ApplicationHandlerStop from None
    state["parts"].append(part)
    if state.get("prompt"):
        with contextlib.suppress(TelegramError):
            await state["prompt"].edit_text(capture_text(state["parts"], state.get("target", "persona")),
                                            parse_mode=ParseMode.HTML,
                                            reply_markup=PERSONA_KB)
    raise ApplicationHandlerStop


async def on_persona_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    await q.answer()
    await finish_capture(context.bot, q.message.chat_id, q.data == "persona:save")


async def view_control(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> None:
    if not await owner_only(update, context):
        return
    chat_id, bot = update.effective_chat.id, context.bot
    track(chat_id, update.message.message_id)
    label = TARGET_LABEL[target]
    p = load_target(target)
    await send_html(bot, chat_id, lines_html(
        f"📜 <b>{label} الحالية</b>",
        f"📏 {len(p):,} حرف · {p.count(chr(10)) + 1:,} سطر",
    ))
    for i in range(0, len(p), 4000):  # النص نفسه بدون أي تنسيق
        with contextlib.suppress(TelegramError):
            m = await bot.send_message(chat_id, p[i : i + 4000] or "—", link_preview_options=NO_PREVIEW)
            track(chat_id, m.message_id)
    fname = "persona.txt" if target == "persona" else "rules.txt"
    m = await bot.send_document(chat_id, InputFile(io.BytesIO(p.encode("utf-8")), filename=fname),
                                caption=f"📄 نسخة طبق الأصل من {label}")
    track(chat_id, m.message_id)


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "persona")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "rules")


# ----- الاختصارات (جمل/ملفات محفوظة) -----

SNIP_NAME = re.compile(r"[\w؀-ۿ][\w؀-ۿ-]{0,39}")
MAX_SNIPPET = 100_000


def expand_snippets(text: str) -> str:
    """يستبدل !الاسم بالنص/الملف المحفوظ. يستبدل المعروف فقط، والباقي يبقى مثل ما هو."""
    snips = load_snippets()
    if not snips or "!" not in text:
        return text

    def repl(m):
        s = snips.get(m.group(1))
        if not s:
            return m.group(0)
        if s.get("type") == "file":
            return f"\n[ملف محفوظ: {s.get('filename') or m.group(1)}]\n{s.get('content', '')}\n"
        return s.get("content", "")

    return re.sub(r"!(" + SNIP_NAME.pattern + r")", repl, text)


async def cmd_snip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """حفظ اختصار: /snip اسم نص... — أو رد على رسالة/ملف بـ /snip اسم."""
    if not await owner_only(update, context):
        return
    msg, bot, chat_id = update.message, context.bot, update.effective_chat.id
    track(chat_id, msg.message_id)
    rest = after_command(msg.text or msg.caption or "")
    parts = rest.split(maxsplit=1)
    if not parts or not SNIP_NAME.fullmatch(parts[0]):
        await send_html(bot, chat_id, lines_html(
            "الاستخدام: <code>/snip اسم النص</code>",
            "أو رد على رسالة أو ملف بـ <code>/snip اسم</code>.",
            "بعدها اكتب <code>!اسم</code> داخل أي رسالة وأنا أفهم إنك تقصده.",
        ))
        return
    name = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    replied = msg.reply_to_message
    doc = msg.document or (replied.document if replied else None)
    try:
        if doc:
            content, typ, fname = await read_persona_file(doc, bot), "file", (doc.file_name or name)
        elif body.strip():
            content, typ, fname = body, "text", None
        elif replied and (replied.text or replied.caption):
            content, typ, fname = (replied.text or replied.caption), "text", None
        else:
            await send_html(bot, chat_id, RLM + "⚠️ اكتب النص بعد الاسم، أو رد على رسالة/ملف.")
            return
    except ValueError as e:
        await send_html(bot, chat_id, RLM + "⚠️ " + esc(str(e)))
        return
    snips = load_snippets()
    snips[name] = {"type": typ, "content": content[:MAX_SNIPPET], "filename": fname}
    save_snippets(snips)
    kind = "ملف" if typ == "file" else "نص"
    await send_html(bot, chat_id, lines_html(
        f"✅ انحفظ الاختصار <code>!{esc(name)}</code> ({kind})",
        f"📏 {len(content):,} حرف",
        f"استخدمه بكتابة <code>!{esc(name)}</code> داخل أي رسالة.",
    ))


async def cmd_snippets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    snips = load_snippets()
    if not snips:
        await send_html(context.bot, chat_id, lines_html(
            "ما فيه اختصارات بعد.",
            "أضف واحد: <code>/snip اسم النص</code>، وبعدها اكتب <code>!اسم</code> في رسالتك.",
        ))
        return
    rows = ["🔖 <b>الاختصارات</b>", ""]
    for name, s in snips.items():
        kind = "📎" if s.get("type") == "file" else "📝"
        preview = " ".join((s.get("content") or "").split())[:40]
        rows.append(f"{kind} <code>!{esc(name)}</code> — {esc(preview)}")
    rows += ["", "للحذف: <code>/delsnip اسم</code>"]
    await send_html(context.bot, chat_id, lines_html(*rows))


async def cmd_delsnip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    name = (context.args or [""])[0]
    snips = load_snippets()
    if name in snips:
        del snips[name]
        save_snippets(snips)
        await send_html(context.bot, chat_id, RLM + f"🗑 انحذف الاختصار <code>!{esc(name)}</code>")
    else:
        await send_html(context.bot, chat_id, RLM + "⚠️ ما فيه اختصار بهذا الاسم. شوف /snippets")


# ----- الإعدادات -----

def settings_html() -> str:
    def show(k):
        v = cfg(k)
        if v is None or v == "default":
            return "<i>افتراضي</i>"
        if isinstance(v, bool):
            return "✅" if v else "❌"
        if k == "provider" and v == "any":
            return "<i>أي مزود (مع بديل تلقائي)</i>"
        return f"<code>{esc(str(v))}</code>"

    rows = ["⚙️ <b>الإعدادات</b>", ""]
    rows += [f"• <b>{desc}:</b> {show(k)}" for k, (_, desc) in PARAMS.items()]
    rows += [f"• <b>GitHub:</b> {'✅ متصل' if github_tools.enabled() else '❌ غير مربوط (أضف GH_PAT)'}"]
    rows += ["", "<i>للأرقام:</i> <code>/set temperature 0.7</code> · <code>/set max_tokens 2000</code> · <code>/set top_p off</code>"]
    return lines_html(*rows)


def settings_keyboard() -> InlineKeyboardMarkup:
    def btn(label, key, value, current):
        return InlineKeyboardButton(("● " if current == value else "") + label, callback_data=f"set:{key}:{value}")

    def toggle(label, key):
        on = cfg(key)
        return InlineKeyboardButton(f"{'✅' if on else '❌'} {label}", callback_data=f"set:{key}:{'off' if on else 'on'}")

    e, prov = cfg("effort"), cfg("provider")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 مستوى التفكير:", callback_data="noop")],
        [btn({"default": "افتراضي"}.get(x, x), "effort", x, e) for x in EFFORTS],
        [InlineKeyboardButton("🔌 المزود:", callback_data="noop")],
        [btn(p.split("/")[0], "provider", p, prov) for p in PROVIDERS],
        [btn("الاثنين", "provider", ",".join(PROVIDERS), prov), btn("أي مزود", "provider", "any", prov)],
        [toggle("التفكير", "show_thinking"), toggle("البحث", "web_search")],
        [toggle("الردود الطويلة كصفحة", "long_pages")],
    ])


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    track(update.effective_chat.id, update.message.message_id)
    await send_html(context.bot, update.effective_chat.id, settings_html(), settings_keyboard())


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.data == "noop":
        await q.answer()
        return
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    _, key, raw = q.data.split(":", 2)
    save_setting(key, parse_value(key, raw))
    await q.answer("تم ✅")
    with contextlib.suppress(BadRequest):
        await q.edit_message_text(settings_html(), parse_mode=ParseMode.HTML, reply_markup=settings_keyboard())


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    args = context.args or []
    if len(args) < 2 or args[0] not in PARAMS:
        await send_html(context.bot, chat_id, lines_html(
            "الاستخدام: <code>/set الإعداد القيمة</code>",
            "الإعدادات: " + " · ".join(f"<code>{k}</code>" for k in PARAMS),
        ))
        return
    try:
        save_setting(args[0], parse_value(args[0], " ".join(args[1:])))
    except ValueError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ قيمة غلط: " + esc(str(e)))
        return
    await send_html(context.bot, chat_id, settings_html(), settings_keyboard())


def shortcut(key: str, value_fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = [key, value_fn(context)]
        await cmd_set(update, context)
    return handler


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)


# ================= التشغيل =================

# خريطة الوظائف: كل "action" في config.json يشير لوظيفة هنا. الأسماء والقوائم في config.json.
def _actions() -> dict:
    return {
        "start": cmd_start,
        "new": cmd_new,
        "settings": cmd_settings,
        "setpersona": cmd_setpersona,
        "persona": cmd_persona,
        "setrules": cmd_setrules,
        "rules": cmd_rules,
        "snip": cmd_snip,
        "snippets": cmd_snippets,
        "delsnip": cmd_delsnip,
        "resetall": cmd_resetall,
        "set": cmd_set,
        "effort": shortcut("effort", lambda c: (c.args or ["default"])[0]),
        "thinking": shortcut("show_thinking", lambda c: "off" if cfg("show_thinking") else "on"),
        "search": shortcut("web_search", lambda c: "off" if cfg("web_search") else "on"),
    }


def _menu_commands(scope: str) -> list:
    """قائمة أوامر "/" حسب النطاق: default = للكل، owner = للمالك."""
    want = {"default": {"both", "default"}, "owner": {"both", "default", "owner"}}[scope]
    out = []
    for c in config.COMMANDS:
        label = c.get("menu_label")
        if label and c.get("menu") in want and c.get("names"):
            out.append(BotCommand(c["names"][0], label))
    return out


async def set_menu(app: Application) -> None:
    # قائمة الأوامر اللي تطلع عند زر "/" في تليجرام — من config.json
    with contextlib.suppress(TelegramError):
        await app.bot.set_my_commands(_menu_commands("default"), scope=BotCommandScopeDefault())
    if OWNER_ID:
        with contextlib.suppress(TelegramError):
            await app.bot.set_my_commands(_menu_commands("owner"), scope=BotCommandScopeChat(OWNER_ID))


def build_app() -> Application:
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(set_menu).concurrent_updates(True)
    if api := os.getenv("TELEGRAM_API_BASE"):  # اختياري: خادم Bot API محلي
        builder = builder.base_url(f"{api}/bot").base_file_url(f"{api}/file/bot")
    app = builder.build()  # concurrent_updates عشان زر الإيقاف يشتغل والرد شغال
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, capture_messages), group=-1)

    # تسجيل الأوامر من config.json
    actions = _actions()
    for c in config.COMMANDS:
        fn = actions.get(c.get("action"))
        if not fn or not c.get("names"):
            log.warning("أمر غير معروف في config.json: %s", c)
            continue
        app.add_handler(CommandHandler(c["names"], fn))
        if c.get("caption"):  # يقبل الأمر مكتوب في وصف ملف/صورة
            app.add_handler(MessageHandler(filters.CaptionRegex(rf"^/{re.escape(c['names'][0])}"), fn))

    # أزرار الكيبورد السفلي
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(NEW_CHAT_BTN)}$"), cmd_new))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(SETTINGS_BTN)}$"), cmd_settings))

    app.add_handler(CallbackQueryHandler(on_stop, pattern=r"^stop$"))
    app.add_handler(CallbackQueryHandler(on_regen, pattern=r"^regen$"))
    app.add_handler(CallbackQueryHandler(on_full_thinking, pattern=r"^think:"))
    app.add_handler(CallbackQueryHandler(on_persona_button, pattern=r"^persona:"))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^(set:|noop$)"))

    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, on_edited_message))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                                   on_user_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if OWNER_ID == 0:
        log.warning("OWNER_ID غير محدد — أوامر المالك لن تعمل لأحد.")
    load_state()
    log.info("البوت شغال عبر OpenRouter بموديل %s والمزود %s", cfg("model"), cfg("provider"))
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

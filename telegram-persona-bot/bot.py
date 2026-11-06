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

import agent
import config
import github_tools
import llm
import render
import telegraph
import vault
from llm import split_think_tags
from render import RLM, esc
from tools import TOOLS, describe_call, run_tool
from workspace import Changes, Checkpoints, Workspace, WorkspaceError, human, kill_all_jobs, valid_name

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
META_FILE = DATA_DIR / "meta.json"          # X-Title (مشفّر)
SNIPPETS_FILE = DATA_DIR / "snippets.json"  # اختصارات المالك (جمل/ملفات محفوظة)
HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCREEN_FILE = DATA_DIR / "screen.json"
TELEGRAPH_FILE = DATA_DIR / "telegraph.json"
# ملفات وضع الوكلاء — تنحسب وقت الاستخدام (DATA_DIR ممكن يتغيّر في الاختبارات)


def dpath(name: str) -> Path:
    return DATA_DIR / name


def ws_base() -> Path:
    return DATA_DIR / "workspaces"


def checkpoints() -> Checkpoints:
    return Checkpoints(dpath("checkpoints.json"))

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

# X-Title ما ينكتب هنا: يتحدد من تليجرام (/settitle) وينحفظ مشفّر، وينرسل مع كل طلب
client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
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
# ذاكرة وضع الوكلاء: منفصلة لكل (شات، مشروع)، وقصيرة — السياق الأقدم في «سجل المشروع» المختصر
AGENT_CFG = config.AGENT
AGENT_HISTORY = int(AGENT_CFG.get("history_messages", 8))
agent_histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=AGENT_HISTORY))
# أرقام الرسائل الظاهرة في كل شات (عشان "محادثة جديدة" تمسح الشاشة)
screen: dict[int, list[int]] = defaultdict(list)


def load_state() -> None:
    for chat_id, msgs in _read_json(HISTORY_FILE, {}).items():
        histories[int(chat_id)].extend(msgs)
    for chat_id, ids in _read_json(SCREEN_FILE, {}).items():
        screen[int(chat_id)] = ids[-1000:]
    for key, msgs in _read_json(dpath("agent_history.json"), {}).items():
        agent_histories[key].extend(msgs)


def save_histories() -> None:
    _write_json(HISTORY_FILE, {str(k): list(v) for k, v in histories.items() if v})


def save_agent_histories() -> None:
    _write_json(dpath("agent_history.json"), {k: list(v) for k, v in agent_histories.items() if v})


def agent_key(chat_id: int, project: str) -> str:
    return f"{chat_id}:{project}"


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
CONFIDENTIAL_NOTE = config.CONFIDENTIAL_NOTE
DEFAULT_PERSONA = config.DEFAULT_PERSONA


def _load_secret(path: Path, key: str, default: str) -> str:
    """يقرأ قيمة مشفّرة ويفكّها. لو مشفّرة وما عندنا المفتاح، يرجّع الافتراضي مع تحذير."""
    data = _read_json(path, {})
    val = data.get(key) if isinstance(data, dict) else None
    if val is None:
        return default
    dec = vault.decrypt(val)
    if dec is None:
        log.warning("ما قدرت أفك تشفير %s (PERSONA_KEY ناقص أو غلط) — استخدمت الافتراضي", key)
        return default
    return dec


def load_persona() -> str:
    return _load_secret(PERSONA_FILE, "persona", DEFAULT_PERSONA)


def save_persona(text: str) -> None:
    _write_json(PERSONA_FILE, {"persona": vault.encrypt(text)})


def load_lock() -> str:
    """قواعد القفل الحالية — المعدّلة من تليجرام إن وُجدت، وإلا من config.json."""
    return _load_secret(RULES_FILE, "lock", config.LOCK_RULES)


def save_lock(text: str) -> None:
    _write_json(RULES_FILE, {"lock": vault.encrypt(text)})


def load_title() -> str:
    """اسم X-Title اللي ينرسل لـ OpenRouter — فاضي = ما ينرسل."""
    return _load_secret(META_FILE, "x_title", "")


def save_title(text: str) -> None:
    _write_json(META_FILE, {"x_title": vault.encrypt(text)})


def load_snippets() -> dict:
    data = _read_json(SNIPPETS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_snippets(data: dict) -> None:
    _write_json(SNIPPETS_FILE, data)


def system_prompt(github: bool = False, confidential: bool = False) -> str:
    extra = GITHUB_NOTE if github else ""
    # لغير المالك: نمنع كشف التعليمات/الهوية. المالك يشوفها عبر /persona و /rules
    head = (CONFIDENTIAL_NOTE + "\n") if confidential and CONFIDENTIAL_NOTE else ""
    return f"{head}{load_lock()}\n{STYLE_RULES}{extra}\n<identity>\n{load_persona()}\n</identity>"


# ================= الإعدادات =================

# مستويات التفكير والإعدادات وقيمها الافتراضية — كلها في config.json
EFFORTS = config.EFFORTS
EFFORT_API = config.EFFORT_API
PARAMS = config.PARAMS
BOOL_KEYS = config.BOOL_KEYS
DEFAULTS = config.DEFAULTS


CODE_DEFAULTS = config.CODE_DEFAULTS
GLOBAL_KEYS = config.GLOBAL_KEYS


def cfg(key: str, mode: str = "chat"):
    """قيمة إعداد. البوت نسختين: mode="chat" (💬) أو "agent" (💻 البرمجة) — لكل وحدة إعداداتها المستقلة."""
    data = _read_json(SETTINGS_FILE, {})
    if mode == "agent" and key not in GLOBAL_KEYS:
        code = data.get("code") if isinstance(data.get("code"), dict) else {}
        value = code.get(key, CODE_DEFAULTS.get(key))
    else:
        value = data.get(key, DEFAULTS.get(key))
    if key == "model" and "/" not in str(value):  # اسم قديم من Moonshot مباشرة
        return DEFAULT_MODEL
    return value


def save_setting(key: str, value, mode: str = "chat") -> None:
    data = _read_json(SETTINGS_FILE, {})
    if mode == "agent" and key not in GLOBAL_KEYS:
        data.setdefault("code", {})[key] = value
    else:
        data[key] = value
    _write_json(SETTINGS_FILE, data)


def owner_mode() -> str:
    """النسخة اللي فاتحها المالك الحين."""
    return "agent" if cfg("agent_mode") else "chat"


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
    if key in ("provider", "worker_provider") and low in ("any", "all", "أي"):
        return "any"
    if low in ("off", "none", "default", "افتراضي"):
        if key == "effort":
            return "default"
        return DEFAULTS[key] if key in ("model", "provider", "worker_model", "worker_provider", "max_agents") else None
    if key == "effort":
        if low not in EFFORTS:
            raise ValueError("القيم: " + " / ".join(EFFORTS))
        return low
    try:
        return typ(raw)
    except ValueError:
        raise ValueError(f"لازم تكون رقم ({typ.__name__})") from None


def request_kwargs(overrides: dict | None = None, mode: str = "chat") -> dict:
    o = overrides or {}

    def get(k):
        return o.get(k, cfg(k, mode))

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
AGENT_BTN = config.AGENT_BTN
CHAT_BTN = config.CHAT_BTN
PROJECT_BTN = config.PROJECT_BTN
CODE_NEW_BTN = config.CODE_NEW_BTN
CODE_SETTINGS_BTN = config.CODE_SETTINGS_BTN
STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف", callback_data="stop")]])
REGEN_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 إعادة التوليد", callback_data="regen")]])


def is_owner_id(user_id: int | None) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and is_owner_id(update.effective_user.id)


def main_keyboard(owner: bool) -> ReplyKeyboardMarkup:
    if not owner:
        return ReplyKeyboardMarkup([[NEW_CHAT_BTN]], resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder="اكتب رسالتك…")
    if cfg("agent_mode"):  # 💻 نسخة البرمجة: شكل مختلف
        return ReplyKeyboardMarkup([[CODE_NEW_BTN, PROJECT_BTN], [CODE_SETTINGS_BTN, CHAT_BTN]],
                                   resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder="💻 اطلب مشروع أو مهمة برمجية…")
    return ReplyKeyboardMarkup([[NEW_CHAT_BTN, SETTINGS_BTN], [AGENT_BTN]], resize_keyboard=True,
                               is_persistent=True, input_field_placeholder="اكتب رسالتك…")


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

    def __init__(self, bot, chat_id: int, show_thinking: bool, long_pages: bool = True,
                 board: "agent.Board | None" = None):
        self.bot, self.chat_id, self.show_thinking = bot, chat_id, show_thinking
        self.long_pages = long_pages
        self.board = board                     # وضع الوكلاء: لوحة العمل (الخطة + الوكلاء)
        self.think = Panel(bot, chat_id)
        self.work = Panel(bot, chat_id)        # بين التفكير والرد، ترتيبها ثابت
        self.reply = Panel(bot, chat_id)
        self.extra_rows: list[list[InlineKeyboardButton]] = []  # أزرار فوق 🔄 في الرد النهائي
        self.inbox: list[str] = []             # رسائل المالك أثناء التنفيذ (توجيه)
        self.final_files: list[dict] = []
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
        if self.board is not None:  # وضع الوكلاء: الرد = كلام آخر جولة بس (اللي قبله ينتقل للسجل)
            self.reply_md = ""
        self._reply_base = self.reply_md

    def demote(self, text: str) -> None:
        """كلام القائد قبل استدعاء أدوات: ينتقل من مكان الرد لسجل الخطوات."""
        if text.strip():
            self.parts.append(["n", text.strip()])
            self._round_part = None
        self.reply_md = ""

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

    def work_pages(self, final: bool) -> list:
        if self.board is None:
            return []
        html = agent.board_html(self.board, final, self.final_files if final else None,
                                "" if final else self.status, self.stopped)
        return [(html, None)] if html else []

    def final_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(self.extra_rows + [[InlineKeyboardButton("🔄 إعادة التوليد", callback_data="regen")]])

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
            return [(body, self.final_kb(), False)]
        full = render.render(md, partial=False)  # كامل بلا قص؛ القرار على الطول الحقيقي
        tail = ("\n\n" + "\n\n".join(extras)) if extras else ""
        if len(full) + len(src_html) + len(tail) <= SINGLE_MAX:  # يكفي رسالة وحدة
            body = full + (("\n\n" + src_html) if src_html else "") + tail
            return [(body, self.final_kb(), False)]
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
                return [(head + tail, self.final_kb(), True)]
            except Exception:
                log.warning("telegraph failed; splitting instead", exc_info=True)
        chunks = render.split_md(md)  # احتياطي: تقسيم — نضمن إن ما ينقص شي
        pages = [[render.page_html(c, False), None, False] for c in chunks]
        if src_html:
            pages.append([src_html, None, False])
        if extras:
            pages[-1][0] += "\n\n" + "\n\n".join(extras)
        pages[-1][1] = self.final_kb()
        return [tuple(p) for p in pages]

    async def flush(self) -> None:
        async with self._lock:
            if time.monotonic() < self._cooldown:
                return
            try:
                await self.think.sync(self.think_pages(False), False)
                await self.work.sync(self.work_pages(False), False)
                await self.reply.sync(self.live_pages(), False)
            except RetryAfter as e:
                self._cooldown = time.monotonic() + e.retry_after
            except TelegramError as e:
                log.warning("flush failed: %s", e)

    async def _loop(self) -> None:
        last_typing = 0.0
        interval = EDIT_INTERVAL * (2 if self.board is not None else 1)  # 3 رسائل تتحدث → أهدى على تليجرام
        while True:
            await asyncio.sleep(interval)
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
            with contextlib.suppress(TelegramError):
                await self.work.sync(self.work_pages(True), True)
            pages = await self.final_reply_pages()
            with contextlib.suppress(TelegramError):
                await self.reply.sync(pages, True)

    def message_ids(self) -> list[int]:
        return self.think.ids() + self.work.ids() + self.reply.ids()


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
    mode: str = "chat"               # chat | agent
    project: str = ""                # وضع الوكلاء: اسم المشروع
    checkpoint: str | None = None    # نقطة الرجوع لتعديلات هذا الطلب


def turn_history(chat_id: int, turn: Turn) -> deque:
    return agent_histories[agent_key(chat_id, turn.project)] if turn.mode == "agent" else histories[chat_id]


def save_turn_history(turn: Turn) -> None:
    (save_agent_histories if turn.mode == "agent" else save_histories)()


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


async def stream_round(ui: ResponseUI, messages: list, kw: dict, tool_kwargs: dict):
    """جولة وحدة مع الموديل. يرجع (نص الرد، التفكير، تفاصيل التفكير، طلبات الأدوات)."""
    headers = {"X-Title": t} if (t := load_title()) else {}

    def on_delta(raw: str, reasoning: str, _calls) -> None:
        tag_think, answer = split_think_tags(raw)
        ui.set_round_reasoning(reasoning + tag_think)
        ui.set_round_answer(answer)

    res = await llm.stream_chat(client, messages, kw, tool_kwargs, headers, on_delta)
    if ui.board is not None:
        ui.board.usage.add(kw["model"], res.usage)
    elif res.usage:
        record_usage({kw["model"]: {**res.usage, "calls": 1}})
    return res.content, res.reasoning, res.details, res.calls


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
    if "tool" in msg and ("support" in msg or "endpoint" in msg):
        return "المزود المحدد ما يدعم الأدوات (وضع البرمجة يحتاجها) — جرّب من ⚙️ الإعدادات: «أي مزود»."
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


async def send_transcript(bot, chat_id: int, turn: "Turn", ui: "ResponseUI") -> int | None:
    """ملف txt بعد الرد: الهوية، رسالة المستخدم، التفكير كامل، والرد. للمالك فقط."""
    text = (
        "هويته:\n" + load_persona() + "\n\n"
        "رسالتي:\n" + (turn.history_text or "") + "\n\n"
        "تفكيره كامل:\n" + (ui._full_thinking().strip() or "—") + "\n\n"
        "رسالته:\n" + (ui.reply_md.strip() or "—") + "\n"
    )
    try:
        m = await bot.send_document(chat_id, InputFile(io.BytesIO(text.encode("utf-8")), filename="رد.txt"),
                                    caption="📄 سجل الرد")
        return m.message_id
    except TelegramError as e:
        log.warning("transcript failed: %s", e)
        return None


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
    messages = [{"role": "system", "content": system_prompt(github_on, confidential=not owner)}, *history,
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

    # ملف txt بعد الرد — للمالك فقط عشان الهوية ما تطلع لأي أحد ثاني
    if owner and overrides.get("export_txt", cfg("export_txt")) and (reply or ui.parts):
        doc_id = await send_transcript(bot, chat_id, turn, ui)
        if doc_id:
            turn.response_ids.insert(0, doc_id)  # ينحذف مع الرد لو ضغطت 🔄، وزر 🔄 يبقى على الرد
            track(chat_id, doc_id)


# ================= وضع الوكلاء =================

session_usage = agent.Usage()          # الاستهلاك من بداية التشغيل الحالي
diffs: "OrderedDict[str, str]" = OrderedDict()      # فرق كل طلب (زر 📄 الفرق)
reports: "OrderedDict[str, str]" = OrderedDict()    # تقارير الوكلاء (زر 🤖 التقارير)
asks: "OrderedDict[str, list[str]]" = OrderedDict()  # خيارات أسئلة القائد (أزرار)


def _remember(store: OrderedDict, key: str, value, keep: int = 40) -> None:
    store[key] = value
    while len(store) > keep:
        store.popitem(last=False)


def new_key() -> str:
    return f"{int(time.time() * 1000) % 10**10}"


def record_usage(models: dict) -> None:
    """يجمع الاستهلاك (التوكنز والتكلفة) لكل موديل: للجلسة الحالية وللإجمالي المحفوظ."""
    if not models:
        return
    data = _read_json(dpath("usage.json"), {})
    data.setdefault("since", time.strftime("%Y-%m-%d"))
    stored = data.setdefault("models", {})
    for model, u in models.items():
        for target in (session_usage.models.setdefault(model, {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}),
                       stored.setdefault(model, {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0})):
            for k in ("prompt", "completion", "cost", "calls"):
                target[k] += u.get(k, 0)
    _write_json(dpath("usage.json"), data)


def current_project() -> str:
    try:
        return valid_name(str(cfg("project", "agent") or "main"))
    except WorkspaceError:
        return "main"


def get_ws(name: str | None = None) -> Workspace:
    try:
        return Workspace(ws_base(), name or current_project()).ensure()
    except WorkspaceError:
        return Workspace(ws_base(), "main").ensure()


def journal_add(project: str, request: str, reply: str, files: list[dict], ok: bool) -> None:
    """سجل المشروع: سطرين لكل طلب — يعطي القائد سياق كل اللي صار بدون ما نرسل المحادثات القديمة كاملة."""
    data = _read_json(dpath("journal.json"), {})
    entries = data.setdefault(project, [])
    entries.append({"t": time.strftime("%Y-%m-%d %H:%M"), "req": " ".join(request.split())[:300],
                    "out": " ".join(reply.split())[:500], "ok": ok,
                    "files": [f"{ {'added': '+', 'deleted': '-', 'modified': '~'}[f['kind']]}{f['path']}" for f in files[:25]]})
    del entries[:-200]
    _write_json(dpath("journal.json"), data)


def journal_text(project: str) -> str:
    entries = _read_json(dpath("journal.json"), {}).get(project) or []
    limit = int(AGENT_CFG.get("journal_chars", 8000))
    lines, used = [], 0
    for e in reversed(entries):  # الأحدث أهم؛ لو طال السجل نختصر القديم
        files = f" | الملفات: {' '.join(e['files'])}" if e.get("files") else ""
        line = f"- [{e['t']}] {'' if e.get('ok', True) else '⚠️ '}الطلب: {e['req']} → {e['out']}{files}"
        if used + len(line) > limit:
            lines.append(f"- … و{len(entries) - len(lines)} طلبات أقدم")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def restore_checkpoint(project: str, cp_id: str | None = None) -> tuple[int, list[str]] | None:
    """يرجّع ملفات المشروع لحالها قبل آخر طلب (لو cp_id هو الأحدث)."""
    store = checkpoints()
    cp = store.pop(project, cp_id)
    if not cp:
        return None
    return Changes.from_json(get_ws(project), cp["files"]).restore()


def lead_kwargs(overrides: dict) -> dict:
    """طلب القائد: نفس شكل المحادثة لكن بإعدادات نسخة البرمجة (افتراضياً تفكير high) + حساب الاستهلاك."""
    kw = request_kwargs(overrides, "agent")
    kw["extra_body"]["usage"] = {"include": True}
    return kw


def worker_kwargs() -> dict:
    extra = {"reasoning": {"enabled": True}, "usage": {"include": True}}
    prov = cfg("worker_provider", "agent")
    if prov and prov != "any":
        extra["provider"] = {"only": [p.strip() for p in str(prov).split(",") if p.strip()], "allow_fallbacks": False}
    return {"model": cfg("worker_model", "agent") or config.WORKER_MODEL, "extra_body": extra}


def make_env(ws: Workspace, changes: Changes, board: "agent.Board", web_on: bool) -> "agent.Env":
    gh_on = github_tools.enabled()
    try:
        parallel = int(cfg("max_agents", "agent") or AGENT_CFG["max_parallel"])
    except (TypeError, ValueError):
        parallel = int(AGENT_CFG["max_parallel"])
    return agent.Env(
        client=client, ws=ws, changes=changes, board=board, worker_kw=worker_kwargs(),
        max_parallel=max(1, min(parallel, 8)), max_tasks=int(AGENT_CFG["max_tasks_per_dispatch"]),
        worker_rounds=int(AGENT_CFG["worker_rounds"]), cmd_timeout=int(AGENT_CFG["command_timeout"]),
        max_timeout=int(AGENT_CFG["max_command_timeout"]),
        web=(lambda n, a: run_tool(n, a)) if web_on else None, web_tools=list(TOOLS) if web_on else [],
        gh=(lambda n, a, progress=None: github_tools.run_github(n, a, progress)) if gh_on else None,
        gh_token=github_tools.token(), gh_login=github_tools.login if gh_on else None,
        headers={"X-Title": t} if (t := load_title()) else {}, worker_prompt=config.WORKER_PROMPT)


async def lead_round(ui: ResponseUI, messages: list, kw: dict, tool_kwargs: dict):
    """جولة للقائد مع إعادة محاولة لو المزود مضغوط (الشغل الطويل ما يضيع بسبب خطأ مؤقت)."""
    for attempt in range(3):
        try:
            return await stream_round(ui, messages, kw, tool_kwargs)
        except Exception as e:
            if attempt == 2 or not agent._transient(e):
                raise
            ui.add_step(f"⏳ ضغط على المزود — إعادة محاولة {attempt + 1}")
            ui.begin_round()
            await asyncio.sleep(4 * (attempt + 1) ** 2)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


async def _generate_agent(bot, chat_id: int, turn: Turn) -> None:
    """وضع الوكلاء: Kimi يقود (يخطط، يقرأ، يكتب، يشغّل، يوزّع على GLM، يراجع)، والرد ملخص الشغل."""
    overrides = turn.overrides
    show_thinking = overrides.get("show_thinking", cfg("show_thinking", "agent"))
    long_pages = overrides.get("long_pages", cfg("long_pages", "agent"))
    plan = bool(overrides.get("plan_mode", cfg("plan_mode", "agent")))
    web_on = bool(overrides.get("web_search", cfg("web_search", "agent")))
    ws = get_ws(turn.project or None)
    turn.project = ws.name
    board = agent.Board(ws.name)
    changes = Changes(ws)
    env = make_env(ws, changes, board, web_on)
    kw = lead_kwargs(overrides)
    gh_on = github_tools.enabled()
    tools = agent.lead_tools(plan, env.web_tools, list(github_tools.GITHUB_TOOLS) if gh_on else [], gh_on)
    history = turn_history(chat_id, turn)
    persona = load_persona() if AGENT_CFG.get("persona_voice", True) else None
    system = agent.lead_system(env, config.LEAD_PROMPT, plan, persona, GITHUB_NOTE, journal_text(ws.name))
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": turn.content}]
    await drop_old_buttons(bot, chat_id)

    ui = ResponseUI(bot, chat_id, show_thinking, long_pages, board=board)
    env.on_status = lambda text: setattr(ui, "status", text)
    ui.task = asyncio.current_task()
    active[chat_id] = ui
    ctx = agent.ToolCtx(env)
    terminal: tuple[str, dict, str] | None = None
    last_text = ""
    max_rounds = int(AGENT_CFG["lead_rounds"])
    try:
        await ui.start()
        for round_no in range(max_rounds + 1):
            if ui.inbox:  # المالك أرسل توجيه وهو يشتغل → يوصل للقائد فوراً
                note = "\n\n".join(ui.inbox)
                ui.inbox.clear()
                messages.append({"role": "user", "content": f"[رسالة من المالك أثناء التنفيذ]\n{note}"})
                turn.history_text += f"\n\n[أثناء التنفيذ]: {note}"
                ui.add_step("📨 توجيه منك: " + " ".join(note.split())[:80])
            if agent.trim_context(messages):
                ctx.seen_reads.clear()
            tool_kwargs = {"tools": tools} if round_no < max_rounds else {"tools": tools, "tool_choice": "none"}
            ui.begin_round()
            content, reasoning, details, calls = await lead_round(ui, messages, kw, tool_kwargs)
            if not calls:
                break
            term = next((c for c in calls if c["name"] in agent.TERMINAL), None)
            if not term:
                ui.demote(content)
            if content.strip():
                last_text = content
            messages.append(llm.assistant_message(content, calls, details))
            for c in calls:
                board.tool_calls += 1
                ui.add_step(agent.describe(c["name"], c["args"]) or describe_tool(c["name"], c["args"]))
                if c["name"] in agent.TERMINAL:
                    terminal = (c["name"], llm.parse_args(c["args"]) or {}, content)
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": "انعرض على المالك."})
                    continue
                result, sources = await agent.execute_lead(env, ctx, c["name"], c["args"])
                ui.add_sources(sources)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                ui.status = ""
            if terminal:
                break
    except asyncio.CancelledError:
        if not ui.stopped:
            raise
        asyncio.current_task().uncancel()  # المالك ضغط ⏹ — نثبت اللي صار
    except Exception as e:
        log.exception("agent turn failed")
        ui.error = error_html(e, True)
    finally:
        ui.finishing = True
        if active.get(chat_id) is ui:
            active.pop(chat_id)

    key = new_key()
    if terminal:
        kind, args, pre = terminal
        lead_in = (pre.strip() + "\n\n") if pre.strip() else ""
        if kind == "ask_user":
            q = str(args.get("question") or "").strip() or "عندي سؤال قبل ما أكمل:"
            opts = [" ".join(str(o).split())[:60] for o in (args.get("options") or []) if str(o).strip()][:5]
            ui.reply_md = lead_in + "❓ " + q
            if opts:
                _remember(asks, key, opts)
                ui.extra_rows += [[_btn(o, f"ask:{key}:{i}")] for i, o in enumerate(opts)]
                ui.reply_md += "\n\n_اختر من الأزرار أو اكتب جوابك._"
        else:
            ui.reply_md = lead_in + (str(args.get("plan") or "").strip() or "(خطة فاضية)")
            ui.extra_rows.append([_btn("✅ نفّذ الخطة", f"plan:go:{ws.name}"[:64]), _btn("✖️ لا", "plan:no")])
    elif not ui.reply_md.strip() and last_text and not ui.error:
        ui.reply_md = last_text  # القائد خلص بدون رد أخير — نعرض آخر كلامه بدل رد فاضي

    files = changes.files()
    ui.final_files = files
    if files:
        checkpoints().push(ws.name, key, turn.history_text, changes)
        turn.checkpoint = key
        _remember(diffs, key, changes.diff())
        ui.extra_rows.append([_btn("↩️ تراجع", f"undo:{key}"), _btn("📄 الفرق", f"diff:{key}"),
                              _btn("📦 المشروع", "zip")])
    if board.agents:
        _remember(reports, key, agent.reports_markdown(board))
        ui.extra_rows.append([_btn(f"🤖 تقارير الوكلاء ({len(board.agents)})", f"rep:{key}")])

    await ui.finish()
    turn.response_ids = ui.message_ids()
    last_turn[chat_id] = turn
    if turn.response_ids:
        regen_msg[chat_id] = turn.response_ids[-1]
    track(chat_id, *turn.response_ids)
    record_usage(board.usage.models)
    reply = ui.reply_md.strip()
    if reply and not ui.error:
        history.append({"role": "user", "content": turn.history_text})
        log_text = agent.work_log(board, files)
        history.append({"role": "assistant", "content": reply + ("\n\n" + log_text if log_text else "")})
        save_agent_histories()
    if reply or files:
        journal_add(ws.name, turn.history_text, reply or "(بدون رد)", files, ok=not (ui.error or ui.stopped))

    if overrides.get("export_txt", cfg("export_txt", "agent")) and (reply or ui.parts):
        doc_id = await send_transcript(bot, chat_id, turn, ui)
        if doc_id:
            turn.response_ids.insert(0, doc_id)
            track(chat_id, doc_id)
    if ui.inbox:  # توجيه وصل بعد آخر جولة → طلب جديد بعد هذا مباشرة
        note = "\n\n".join(ui.inbox)
        asyncio.create_task(generate(bot, chat_id, Turn(None, note, note, {}, turn.user_id,
                                                         mode="agent", project=ws.name)))


async def run_turn(bot, chat_id: int, turn: Turn) -> None:
    """لازم يكون قفل الشات ماسك."""
    if turn.mode == "agent":
        await _generate_agent(bot, chat_id, turn)
    else:
        await _generate(bot, chat_id, turn)


async def generate(bot, chat_id: int, turn: Turn) -> None:
    async with chat_locks[chat_id]:  # الرسائل الجديدة تنتظر دورها
        await run_turn(bot, chat_id, turn)


async def redo(bot, chat_id: int, user_msg_id: int | None, replacement: Turn | None = None) -> bool:
    """يعيد آخر رد (🔄) أو يرد من جديد على رسالة انعدلت."""
    ui = active.get(chat_id)
    if ui:
        ui.stop()
    async with chat_locks[chat_id]:
        turn = last_turn.get(chat_id)
        if not turn or turn.user_msg_id != user_msg_id:
            return False
        history = turn_history(chat_id, turn)
        if len(history) >= 2 and history[-2].get("content") == turn.history_text and history[-1]["role"] == "assistant":
            history.pop()
            history.pop()
            save_turn_history(turn)
        if turn.mode == "agent" and turn.checkpoint:  # 🔄 في وضع الوكلاء = نرجّع الملفات ونعيد من نفس النقطة
            restore_checkpoint(turn.project, turn.checkpoint)
        await delete_many(bot, chat_id, turn.response_ids)
        untrack(chat_id, turn.response_ids)
        last_turn.pop(chat_id, None)
        new = replacement or Turn(turn.user_msg_id, turn.content, turn.history_text, turn.overrides, turn.user_id)
        new.mode, new.project = turn.mode, turn.project
        await run_turn(bot, chat_id, new)
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
    notes: list[str] = field(default_factory=list)   # وضع الوكلاء: ملفات انحفظت في المشروع


async def download(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


async def build_item(msg: Message, bot, ws: Workspace | None = None) -> Item | str:
    """يحول رسالة المستخدم لمحتوى. يرجع نص خطأ لو النوع ما ينفع.
    ws (وضع الوكلاء للمالك): الملفات تنحفظ داخل المشروع — أي نوع، وzip ينفك."""
    item = Item(msg=msg, text=msg.text or msg.caption or "")
    if ws is not None and msg.document:
        doc = msg.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            return "الملف كبير، حد تليجرام للبوتات 20 ميقا."
        data = await download(bot, doc.file_id)
        try:
            item.notes.append(await asyncio.to_thread(ws.save_upload, doc.file_name or "file", data))
        except WorkspaceError as e:
            return str(e)
        name = doc.file_name or "file"
        if (doc.mime_type or "").startswith("image/"):
            item.images.append(f"data:{doc.mime_type};base64," + base64.b64encode(data).decode())
        elif not name.lower().endswith(".zip") and (text := decode_text(data)) is not None and len(text) <= 6000:
            item.files.append((name, text))  # ملف صغير: نرسله مباشرة ونوفّر جولة قراءة
        return item
    if msg.photo:
        data = await download(bot, msg.photo[-1].file_id)
        item.images.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
        if ws is not None:
            with contextlib.suppress(WorkspaceError, OSError):
                item.notes.append(ws.save_upload(f"photo_{msg.message_id}.jpg", data))
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
    notes = [n for it in items for n in it.notes]
    if not text and not images and not files and not notes:
        return None
    agent_turn = owner and cfg("agent_mode")
    prefix = quote_context(items[0].msg)
    if notes:
        prefix += "".join(f"[📎 المالك رفع للمشروع: {n}]\n" for n in notes)
    mentions = mention_files(text) if agent_turn and text else ""  # للموديل بس، ما ينحفظ بالذاكرة
    file_blocks = "".join(f"📎 الملف: {name}\n```\n{body[:MAX_FILE_CHARS]}\n```\n\n" for name, body in files)
    full = prefix + mentions + file_blocks + text
    content: object = full
    if images:
        content = [{"type": "text", "text": full or "وش تشوف في الصورة؟"}] + \
                  [{"type": "image_url", "image_url": {"url": u}} for u in images]
    if agent_turn:  # الملف محفوظ في المشروع — الذاكرة تحتاج اسمه بس
        saved = "".join(f"📎 الملف: {n}\n" for n, _ in files)
    else:
        saved = "".join(f"📎 الملف: {n}\n```\n{b[:20_000]}\n```\n\n" for n, b in files)
    history_text = prefix + "".join("[🖼 صورة] " for _ in images) + saved + text
    if agent_turn and THINK_HARDER.search(text) and "effort" not in overrides:
        overrides["effort"] = "max"  # «ultrathink» / «فكر بعمق» مثل Claude Code
    return Turn(user_msg_id=items[-1].msg.message_id, content=content, history_text=history_text.strip(),
                overrides=overrides, user_id=items[0].msg.from_user.id if items[0].msg.from_user else None,
                mode="agent" if agent_turn else "chat", project=current_project() if agent_turn else "")


THINK_HARDER = re.compile(r"ultrathink|think\s+(very\s+)?hard|فكر\s*(بعمق|زين|كويس)|فكّر\s*(بعمق|زين)", re.I)
_MENTION = re.compile(r"(?<![\w@])@([\w./\-]+\.[\w]+|[\w.\-]+/[\w./\-]*)")


def mention_files(text: str) -> str:
    """@مسار داخل الرسالة (وضع الوكلاء) → نرفق محتوى الملف، أو شجرة المجلد، مثل Claude Code."""
    ws = get_ws()
    out, used = [], 0
    for m in _MENTION.finditer(text):
        path = m.group(1).rstrip(".,،")
        try:
            full = ws.resolve(path)
        except WorkspaceError:
            continue
        if full.is_file():
            body = ws.read(path, 1, 800)
        elif full.is_dir():
            body = ws.list_files("**/*", path)
        else:
            continue
        if used + len(body) > 40_000:
            break
        used += len(body)
        out.append(f"[محتوى @{path}]\n{body}\n")
    return "".join(out)


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
    agent_on = is_owner(update) and cfg("agent_mode")
    try:
        item = await build_item(msg, context.bot, get_ws() if agent_on else None)
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

    # وضع الوكلاء شغّال على طلب: رسالة نصية من المالك = توجيه يوصل للقائد بالجولة الجاية (مثل Claude Code)
    running = active.get(chat_id)
    if (running is not None and running.board is not None and is_owner(update) and not running.finishing
            and item.text and not (item.images or item.files or item.notes) and not entry and not looks_split):
        running.inbox.append(item.text)
        await send_html(context.bot, chat_id, RLM + "📨 <i>وصلت — بيشوفها القائد بالخطوة الجاية بدون ما يوقف الشغل.</i>")
        return
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
            "",
            "💻 <b>وضع البرمجة</b> (مثل Claude Code) — /code",
            "القائد يخطط ويوزّع الشغل على وكلاء بالتوازي، يكتب ويشغّل ويختبر داخل مشروع حقيقي،",
            "ويتحكم بـ GitHub ويتابع CI ويصلّح الأخطاء. /project /files /zip /undo /sh /watch /plan /cost",
        )
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    if is_owner(update) and cfg("agent_mode"):
        await send_html(context.bot, chat_id, code_welcome_html(), main_keyboard(True))
        return
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
        last_turn.pop(chat_id, None)
        if is_owner(update) and cfg("agent_mode"):
            agent_histories.pop(agent_key(chat_id, current_project()), None)
            save_agent_histories()
            await send_html(context.bot, chat_id, lines_html(
                "✨ <b>جلسة برمجة جديدة</b>",
                f"ملفات المشروع <code>{esc(current_project())}</code> وسجله المختصر باقية — بس المحادثة بدأت من جديد.",
            ), main_keyboard(True))
            return
        histories.pop(chat_id, None)
        save_histories()
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
    agent_histories.clear()
    save_agent_histories()
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
    if text in (NEW_CHAT_BTN, SETTINGS_BTN, AGENT_BTN, CHAT_BTN, PROJECT_BTN, CODE_NEW_BTN, CODE_SETTINGS_BTN) or \
            (text or "").startswith(("/setpersona", "/setrules")):
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


async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """X-Title اللي ينرسل لـ OpenRouter. ينحفظ مشفّر — ما يظهر في المستودع."""
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    raw = after_command(update.message.text or "").strip()
    if not raw:
        await send_html(context.bot, chat_id, lines_html(
            "الاستخدام: <code>/settitle اسم التطبيق</code> (بالإنجليزي)",
            "ولإيقافه: <code>/settitle off</code>",
        ))
        return
    if raw.lower() in ("off", "none", "remove", "حذف", "ايقاف", "إيقاف"):
        save_title("")
        await send_html(context.bot, chat_id, RLM + "🗑 انشال X-Title — ما عاد ينرسل.")
        return
    title = " ".join(raw.split())  # سطر واحد بدون فواصل أسطر
    if not title.isascii():
        await send_html(context.bot, chat_id, lines_html(
            "⚠️ X-Title لازم يكون <b>بالإنجليزي</b> (حروف وأرقام ورموز).",
            "ترويسات HTTP ما تقبل الحروف العربية، والطلب بيفشل لو انكتب بالعربي.",
        ))
        return
    save_title(title[:100])
    note = "🔒 محفوظ مشفّر" if vault.enabled() else "⚠️ غير مشفّر — أضف PERSONA_KEY عشان ينحفظ مشفّر"
    await send_html(context.bot, chat_id, lines_html(
        f"✅ انحفظ X-Title: <code>{esc(title[:100])}</code>",
        note,
    ))


async def cmd_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    t = load_title()
    await send_html(context.bot, chat_id, RLM + (f"🏷 X-Title الحالي: <code>{esc(t)}</code>" if t
                                                  else "🏷 ما فيه X-Title (ما ينرسل). حدّده بـ /settitle"))


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

def mode_params(mode: str) -> list[str]:
    """إعدادات النسخة: المحادثة أو البرمجة (المشتركة global ما تنعرض كإعداد)."""
    want = "code" if mode == "agent" else "chat"
    return [k for k in PARAMS if want in config.PARAM_MODES.get(k, ("chat", "code"))]


def param_label(key: str, mode: str) -> str:
    return config.CODE_LABELS.get(key, PARAMS[key][1]) if mode == "agent" else PARAMS[key][1]


def settings_html(mode: str = "chat") -> str:
    def show(k):
        v = cfg(k, mode)
        if v is None or v == "default":
            return "<i>افتراضي</i>"
        if isinstance(v, bool):
            return "✅" if v else "❌"
        if k in ("provider", "worker_provider") and v == "any":
            return "<i>أي مزود (مع بديل تلقائي)</i>"
        return f"<code>{esc(str(v))}</code>"

    title = "💻 <b>إعدادات وضع البرمجة</b>" if mode == "agent" else "⚙️ <b>الإعدادات</b>"
    rows = [title, ""]
    rows += [f"• <b>{param_label(k, mode)}:</b> {show(k)}" for k in mode_params(mode)]
    rows += [f"• <b>GitHub:</b> {'✅ متصل' if github_tools.enabled() else '❌ غير مربوط (أضف GH_PAT)'}"]
    if mode == "agent":
        rows += [f"• <b>📁 المشروع الحالي:</b> <code>{esc(current_project())}</code>",
                 "", "<i>إعدادات البرمجة مستقلة عن المحادثة — تعديل وحدة ما يأثر على الثانية.</i>"]
    rows += ["", "<i>للأرقام:</i> <code>/set temperature 0.7</code> · <code>/set max_tokens 2000</code> · <code>/set top_p off</code>"]
    return lines_html(*rows)


def settings_keyboard(mode: str = "chat") -> InlineKeyboardMarkup:
    tag = "cset" if mode == "agent" else "set"   # الزر يعرف أي نسخة يعدّل حتى لو تغيّر الوضع بعدين

    def btn(label, key, value, current):
        return InlineKeyboardButton(("● " if current == value else "") + label, callback_data=f"{tag}:{key}:{value}")

    def toggle(label, key):
        on = cfg(key, mode)
        return InlineKeyboardButton(f"{'✅' if on else '❌'} {label}", callback_data=f"{tag}:{key}:{'off' if on else 'on'}")

    def head(text):
        return [InlineKeyboardButton(text, callback_data="noop")]

    e, prov = cfg("effort", mode), cfg("provider", mode)
    if mode != "agent":
        return InlineKeyboardMarkup([
            head("🧠 مستوى التفكير:"),
            [btn({"default": "افتراضي"}.get(x, x), "effort", x, e) for x in EFFORTS],
            head("🔌 المزود:"),
            [btn(p.split("/")[0], "provider", p, prov) for p in PROVIDERS],
            [btn("الاثنين", "provider", ",".join(PROVIDERS), prov), btn("أي مزود", "provider", "any", prov)],
            [toggle("التفكير", "show_thinking"), toggle("البحث", "web_search")],
            [toggle("الردود الطويلة كصفحة", "long_pages")],
            [toggle("ملف txt بعد كل رد", "export_txt")],
            [InlineKeyboardButton("💻 التحويل لوضع البرمجة", callback_data="set:agent_mode:on")],
        ])
    wp = cfg("worker_provider", mode)
    return InlineKeyboardMarkup([
        head("🧠 تفكير القائد:"),
        [btn({"default": "افتراضي"}.get(x, x), "effort", x, e) for x in EFFORTS],
        head("🔌 مزود القائد:"),
        [btn(p.split("/")[0], "provider", p, prov) for p in PROVIDERS],
        [btn("الاثنين", "provider", ",".join(PROVIDERS), prov), btn("أي مزود", "provider", "any", prov)],
        head("👷 الوكلاء بالتوازي:"),
        [btn(str(n), "max_agents", n, cfg("max_agents", mode)) for n in (2, 3, 4, 6, 8)],
        head("🔌 مزود الوكلاء:"),
        [btn(p.split("/")[0], "worker_provider", p, wp) for p in config.WORKER_PROVIDERS[:3]]
        + [btn("أي مزود", "worker_provider", "any", wp)],
        [toggle("📝 الخطة أولاً", "plan_mode"), toggle("التفكير", "show_thinking")],
        [toggle("البحث", "web_search"), toggle("الطويل كصفحة", "long_pages")],
        [toggle("ملف txt بعد كل رد", "export_txt")],
        [InlineKeyboardButton("💬 الرجوع لوضع المحادثة", callback_data="set:agent_mode:off")],
    ])


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    track(update.effective_chat.id, update.message.message_id)
    mode = owner_mode()
    await send_html(context.bot, update.effective_chat.id, settings_html(mode), settings_keyboard(mode))


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.data == "noop":
        await q.answer()
        return
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    tag, key, raw = q.data.split(":", 2)
    mode = "agent" if tag == "cset" else "chat"
    if key not in PARAMS:
        await q.answer()
        return
    save_setting(key, parse_value(key, raw), mode)
    await q.answer("تم ✅")
    if key == "agent_mode":  # تبديل النسخة: الكيبورد والقائمة واللوحة كلها تتغيّر
        await switch_mode(context.bot, q.message.chat_id, bool(cfg("agent_mode")), q.message)
        return
    with contextlib.suppress(BadRequest):
        await q.edit_message_text(settings_html(mode), parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(mode))


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    mode = owner_mode()
    args = context.args or []
    keys = mode_params(mode)
    if len(args) < 2 or args[0] not in keys:
        await send_html(context.bot, chat_id, lines_html(
            "الاستخدام: <code>/set الإعداد القيمة</code>",
            "الإعدادات: " + " · ".join(f"<code>{k}</code>" for k in keys),
        ))
        return
    try:
        save_setting(args[0], parse_value(args[0], " ".join(args[1:])), mode)
    except ValueError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ قيمة غلط: " + esc(str(e)))
        return
    await send_html(context.bot, chat_id, settings_html(mode), settings_keyboard(mode))


def shortcut(key: str, value_fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = [key, value_fn(context)]
        await cmd_set(update, context)
    return handler


# ----- وضع الوكلاء: الأوامر -----

def _max_agents() -> int:
    try:
        return max(1, min(int(cfg("max_agents", "agent") or AGENT_CFG["max_parallel"]), 8))
    except (TypeError, ValueError):
        return int(AGENT_CFG["max_parallel"])


async def _owner_cmd(update: Update, context) -> int | None:
    """فحص المالك + تسجيل رسالة الأمر. يرجّع chat_id أو None."""
    if not await owner_only(update, context):
        return None
    chat_id = update.effective_chat.id
    if update.message:
        track(chat_id, update.message.message_id)
    return chat_id


def code_welcome_html() -> str:
    ws = get_ws()
    n, size = ws.stats()
    return lines_html(
        "💻 <b>وضع البرمجة</b>",
        f"🧠 القائد: <code>{esc(str(cfg('model', 'agent')))}</code> · تفكير {esc(str(cfg('effort', 'agent')))}",
        f"👷 المنفّذين: <code>{esc(str(cfg('worker_model', 'agent')))}</code> — حتى {_max_agents()} بالتوازي",
        f"📁 المشروع: <code>{esc(ws.name)}</code> ({n} ملف · {human(size)}) — /project للتبديل",
        "",
        "اطلب أي مشروع أو مهمة برمجية: أفهم، أخطط، أوزّع الشغل على الوكلاء، أكتب وأشغّل وأختبر، وأرجع لك بالنتيجة.",
        "• أرسل ملفات أو <code>zip</code> وتنحفظ في المشروع · <code>@مسار</code> يرفق ملف من المشروع",
        "• اكتب وهو شغّال وتوصل رسالتك للقائد فوراً · ⏹ يوقف · «فكر بعمق» أو ultrathink = تفكير أقصى",
        "• /plan خطة قبل التنفيذ · /undo تراجع · /files /zip · /sh أمر · /watch متابعة CI",
        "• /init يكتب ذاكرة المشروع · /review مراجعة · /context /status /cost",
        "",
        "<i>هذي نسخة البرمجة: إعداداتها وشكلها مستقلين — الهوية والاختصارات وGitHub مشتركة مع المحادثة.</i>",
    )


async def refresh_owner_menu(bot) -> None:
    """قائمة «/» عند المالك تتغيّر مع النسخة."""
    if OWNER_ID:
        with contextlib.suppress(TelegramError):
            await bot.set_my_commands(_menu_commands("code" if cfg("agent_mode") else "owner"),
                                      scope=BotCommandScopeChat(OWNER_ID))


async def switch_mode(bot, chat_id: int, on: bool, settings_msg: Message | None = None) -> None:
    save_setting("agent_mode", on)
    await refresh_owner_menu(bot)
    if settings_msg is not None:  # لوحة الإعدادات المفتوحة تتحول للوحة النسخة الثانية
        mode = owner_mode()
        with contextlib.suppress(TelegramError):
            await settings_msg.edit_text(settings_html(mode), parse_mode=ParseMode.HTML,
                                         reply_markup=settings_keyboard(mode))
    if on:
        await send_html(bot, chat_id, code_welcome_html(), main_keyboard(True))
    else:
        await send_html(bot, chat_id, lines_html(
            "💬 <b>وضع المحادثة</b>",
            "رجعنا للمحادثة العادية بإعداداتها. مشاريع البرمجة وذاكرتها محفوظة — ترجع لها بـ /code.",
        ), main_keyboard(True))


async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await switch_mode(context.bot, chat_id, True)


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await switch_mode(context.bot, chat_id, False)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    on = not cfg("plan_mode", "agent")
    save_setting("plan_mode", on, "agent")
    await send_html(context.bot, chat_id, lines_html(
        "📝 <b>وضع التخطيط شغّال</b>" if on else "⚡️ <b>وضع التخطيط مقفل</b>",
        "القائد يستكشف ويقدّم خطة، وما ينفّذ إلا لما توافق ✅" if on else "القائد ينفّذ مباشرة.",
    ))


def list_projects() -> list[str]:
    base = ws_base()
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def projects_html() -> str:
    cur = current_project()
    rows = [f"📁 <b>المشروع الحالي:</b> <code>{esc(cur)}</code>", ""]
    for name in list_projects()[:30]:
        n, size = Workspace(ws_base(), name).stats()
        cps = checkpoints().count(name)
        rows.append(f"{'●' if name == cur else '○'} <code>{esc(name)}</code> — {n} ملف · {human(size)}"
                    + (f" · ↩️ {cps}" if cps else ""))
    rows += ["", "تبديل/إنشاء: <code>/project الاسم</code> · حذف: <code>/project delete الاسم</code>",
             "استنساخ من GitHub: <code>/clone owner/repo</code>"]
    return lines_html(*rows)


def projects_kb() -> InlineKeyboardMarkup | None:
    cur = current_project()
    rows = [[_btn(("● " if n == cur else "📁 ") + n, f"proj:{n}")] for n in list_projects()[:20]
            if len(f"proj:{n}".encode()) <= 64]
    return InlineKeyboardMarkup(rows) if rows else None


async def switch_project(bot, chat_id: int, name: str) -> None:
    ws = get_ws(name)
    save_setting("project", ws.name, "agent")
    n, size = ws.stats()
    await send_html(bot, chat_id, lines_html(
        f"📁 <b>المشروع:</b> <code>{esc(ws.name)}</code>",
        f"{n} ملف · {human(size)}" if n else "مشروع فاضي — جاهز تبدأ فيه.",
        "" if cfg("agent_mode") else "شغّل /code عشان تشتغل عليه.",
    ))


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args:
        get_ws()  # المشروع الحالي يكون موجود
        await send_html(context.bot, chat_id, projects_html(), projects_kb())
        return
    try:
        if args[0].lower() in ("delete", "rm", "حذف") and len(args) > 1:
            name = valid_name(args[1])
            if name not in list_projects():
                raise WorkspaceError("ما فيه مشروع بهذا الاسم")
            await send_html(context.bot, chat_id, lines_html(
                f"🗑 متأكد تبي تحذف المشروع <code>{esc(name)}</code> بكل ملفاته؟", "ما ينفع التراجع بعدها."),
                InlineKeyboardMarkup([[_btn("🗑 احذف نهائياً", f"projdel:{name}"[:64]), _btn("✖️ لا", "noop")]]))
            return
        await switch_project(context.bot, chat_id, valid_name(args[0]))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ " + esc(str(e)))


async def on_project_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    kind, _, name = q.data.partition(":")
    await q.answer()
    try:
        name = valid_name(name)
    except WorkspaceError:
        return
    if kind == "proj":
        await switch_project(context.bot, q.message.chat_id, name)
        return
    import shutil
    shutil.rmtree(ws_base() / name, ignore_errors=True)
    checkpoints().drop_project(name)
    data = _read_json(dpath("journal.json"), {})
    if data.pop(name, None) is not None:
        _write_json(dpath("journal.json"), data)
    for k in [k for k in agent_histories if k.endswith(f":{name}")]:
        agent_histories.pop(k)
    save_agent_histories()
    if current_project() == name:
        save_setting("project", "main", "agent")
    with contextlib.suppress(TelegramError):
        await q.edit_message_reply_markup(None)
    await send_html(context.bot, q.message.chat_id, RLM + f"🗑 انحذف المشروع <code>{esc(name)}</code>.")


async def send_file(bot, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
    try:
        m = await bot.send_document(chat_id, InputFile(io.BytesIO(data), filename=filename), caption=caption or None)
        track(chat_id, m.message_id)
    except TelegramError as e:
        await send_html(bot, chat_id, RLM + "⚠️ ما قدرت أرسل الملف: " + esc(str(e)))


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    path = " ".join(context.args or []) or "."
    try:
        text = ws.tree(400) if path == "." else ws.list_files("**/*", path)
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ " + esc(str(e)))
        return
    n, size = ws.stats()
    head = RLM + f"🗂 <b>{esc(ws.name)}</b> — {n} ملف · {human(size)}"
    if len(text) > 3500:
        await send_html(context.bot, chat_id, head)
        await send_file(context.bot, chat_id, text.encode(), f"{ws.name}-files.txt")
    else:
        await send_html(context.bot, chat_id, f"{head}\n<pre>{esc(text)}</pre>")


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    path = " ".join(context.args or [])
    try:
        full = ws.resolve(path)
        if not path or not full.is_file():
            raise WorkspaceError("اكتب مسار ملف موجود: <code>/get src/app.py</code>")
        if full.stat().st_size > 45_000_000:
            raise WorkspaceError("الملف أكبر من حد تليجرام")
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ " + str(e))
        return
    await send_file(context.bot, chat_id, full.read_bytes(), full.name, f"📄 {ws.rel(full)}")


async def send_zip(bot, chat_id: int) -> None:
    ws = get_ws()
    try:
        data = await asyncio.to_thread(ws.zip_bytes)
    except WorkspaceError as e:
        await send_html(bot, chat_id, RLM + "⚠️ " + esc(str(e)))
        return
    n, _ = ws.stats()
    await send_file(bot, chat_id, data, f"{ws.name}.zip", f"📦 {ws.name} — {n} ملف")


async def cmd_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await send_zip(context.bot, chat_id)


async def cmd_sh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تشغيل أمر مباشرة في المشروع (مثل ! في Claude Code)."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    command = after_command(update.message.text or "").strip()
    if not command:
        await send_html(context.bot, chat_id, RLM + "الاستخدام: <code>/sh ls -la</code> — يشتغل داخل مجلد المشروع")
        return
    if agent._DANGER.search(command):
        await send_html(context.bot, chat_id, RLM + "⛔ أمر خطير مرفوض.")
        return
    ws = get_ws()
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    r = await ws.run(command, 300)
    status = "⏱ انتهت المهلة" if r["timed_out"] else ("✅" if r["code"] == 0 else f"❌ exit {r['code']}")
    out = r["output"].strip() or "(بدون مخرجات)"
    head = RLM + f"{status} · <code>{esc(command[:200])}</code> · {r['seconds']:.1f}ث"
    if len(out) > 3300:
        await send_html(context.bot, chat_id, head)
        await send_file(context.bot, chat_id, out.encode(), "output.txt")
    else:
        await send_html(context.bot, chat_id, f"{head}\n<pre>{esc(out)}</pre>")


async def do_undo(bot, chat_id: int, cp_id: str | None = None) -> None:
    project = current_project()
    top = checkpoints().peek(project)
    if not top:
        await send_html(bot, chat_id, RLM + "ما فيه تعديلات أقدر أتراجع عنها في هذا المشروع.")
        return
    if cp_id and top["id"] != cp_id:
        await send_html(bot, chat_id, lines_html("⚠️ فيه تعديلات أحدث من هذي.",
                                                 "استخدم /undo لين توصل لها (كل مرة يرجع طلب واحد)."))
        return
    restored, skipped = restore_checkpoint(project, top["id"]) or (0, [])
    history = agent_histories[agent_key(chat_id, project)]
    label = top.get("label", "")
    if len(history) >= 2 and history[-1].get("role") == "assistant" \
            and str(history[-2].get("content", ""))[:120] == label:
        history.pop()  # الطلب اللي تراجعنا عنه ما يبقى في ذاكرة القائد
        history.pop()
        save_agent_histories()
    if (t := last_turn.get(chat_id)) and t.checkpoint == top["id"]:
        t.checkpoint = None
    left = checkpoints().count(project)
    await send_html(bot, chat_id, lines_html(
        f"↩️ <b>تراجعت عن:</b> {esc(top.get('label', '')[:100])}",
        f"رجّعت {restored} ملف لحالها قبل الطلب" + (f" · ما قدرت أرجّع {len(skipped)} (كبيرة)" if skipped else ""),
        "<i>تغييرات الأوامر (تثبيت، حذف بالأوامر…) ما تنحسب، مثل Claude Code.</i>",
        f"باقي {left} نقطة رجوع." if left else "",
    ))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await do_undo(context.bot, chat_id)


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    if not diffs:
        await send_html(context.bot, chat_id, RLM + "ما فيه تعديلات بعد.")
        return
    await send_file(context.bot, chat_id, next(reversed(diffs.values())).encode(), "changes.diff", "📄 فرق آخر طلب")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ذاكرة المشروع الدائمة (AGENTS.md) — تنقرأ للقائد والوكلاء كل مرة، مثل CLAUDE.md."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    f = ws.root / "AGENTS.md"
    note = after_command(update.message.text or "").strip()
    if note:
        cur = f.read_text(encoding="utf-8") if f.is_file() else "# AGENTS.md — ذاكرة المشروع\n"
        f.write_text(cur.rstrip("\n") + f"\n- {note}\n", encoding="utf-8")
        await send_html(context.bot, chat_id, RLM + "🧠 انضافت لذاكرة المشروع (AGENTS.md).")
        return
    if not f.is_file():
        await send_html(context.bot, chat_id, lines_html(
            "ما فيه ذاكرة مشروع بعد.", "أضف: <code>/memory استخدم pytest للاختبارات</code>",
            "أو خل القائد يكتبها بتحليل المشروع: /init"))
        return
    text = f.read_text(encoding="utf-8", errors="replace")
    if len(text) > 3500:
        await send_file(context.bot, chat_id, text.encode(), "AGENTS.md", "🧠 ذاكرة المشروع")
    else:
        await send_html(context.bot, chat_id, RLM + "🧠 <b>AGENTS.md</b>\n<pre>" + esc(text) + "</pre>")


async def summarize_messages(msgs: list) -> str:
    def body(m):
        c = m.get("content")
        return c if isinstance(c, str) else "[محتوى غير نصي]"
    transcript = "\n\n".join(f"{'المستخدم' if m['role'] == 'user' else 'المساعد'}: {body(m)}" for m in msgs)
    kw = worker_kwargs()
    res = await llm.stream_chat(client, [
        {"role": "system", "content": "لخّص المحادثة التالية تلخيص كثيف ودقيق يحفظ كل اللي يلزم لإكمالها: "
                                      "الأهداف، القرارات، التفاصيل التقنية والأسماء والأرقام، وش انعمل وش باقي. "
                                      "بنفس لغة المحادثة، نقاط مختصرة، بدون مقدمات."},
        {"role": "user", "content": agent.clip(transcript, 150_000)}], kw)
    record_usage({kw["model"]: {**(res.usage or {}), "calls": 1}})
    return res.content.strip()


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يضغط ذاكرة المحادثة الحالية لملخص (يوفّر توكنز ويحفظ السياق)، مثل /compact."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    agent_on = bool(cfg("agent_mode"))
    async with chat_locks[chat_id]:
        hist = agent_histories[agent_key(chat_id, current_project())] if agent_on else histories[chat_id]
        if len(hist) < 4:
            await send_html(context.bot, chat_id, RLM + "المحادثة قصيرة — ما تحتاج ضغط.")
            return
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        before = sum(len(str(m.get("content"))) for m in hist)
        try:
            summary = await summarize_messages(list(hist))
        except Exception as e:
            await send_html(context.bot, chat_id, RLM + "⚠️ ما قدرت ألخّص: " + esc(str(e)[:200]))
            return
        if not summary:
            return
        hist.clear()
        hist.append({"role": "user", "content": "[ملخص المحادثة السابقة — للسياق]"})
        hist.append({"role": "assistant", "content": summary})
        (save_agent_histories if agent_on else save_histories)()
        last_turn.pop(chat_id, None)
    await send_html(context.bot, chat_id, lines_html(
        "🗜 <b>انضغطت المحادثة</b>", f"{before:,} حرف ← {len(summary):,} حرف — السياق محفوظ كملخص."))


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    data = _read_json(dpath("usage.json"), {})
    rows = ["💰 <b>الاستهلاك</b>", "", "<b>من بداية التشغيل الحالي:</b>"]
    rows += [f"• <code>{esc(m)}</code>: {agent.usage_line(u)}" for m, u in session_usage.models.items()] or ["—"]
    rows += ["", f"<b>الإجمالي (من {esc(data.get('since', '—'))}):</b>"]
    total = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
    for m, u in (data.get("models") or {}).items():
        rows.append(f"• <code>{esc(m)}</code>: {agent.usage_line(u)}")
        for k in total:
            total[k] += u.get(k, 0)
    rows += ["", f"<b>المجموع:</b> {agent.usage_line(total)}",
             "<i>التكلفة حسب اللي يرجّعه OpenRouter مع كل طلب.</i>"]
    await send_html(context.bot, chat_id, lines_html(*rows))


async def cmd_clone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args or not github_tools.enabled():
        await send_html(context.bot, chat_id, RLM + ("الاستخدام: <code>/clone owner/repo [branch]</code> — يصير مشروع جديد"
                                                     if github_tools.enabled() else "⚠️ GitHub غير مربوط (GH_PAT)."))
        return
    try:
        repo = args[0].removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if "/" not in repo:
            repo = f"{await github_tools.login()}/{repo}"
        ws = get_ws(valid_name(repo.split("/")[-1]))
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        msg = await ws.clone(repo, args[1] if len(args) > 1 else None, github_tools.token())
        save_setting("project", ws.name, "agent")
        await send_html(context.bot, chat_id, lines_html(f"⬇️ {esc(msg)}", f"📁 المشروع الحالي: <code>{esc(ws.name)}</code>"))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ " + esc(str(e)))


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args or not github_tools.enabled():
        await send_html(context.bot, chat_id, RLM + ("الاستخدام: <code>/push owner/repo [branch] [رسالة]</code>"
                                                     if github_tools.enabled() else "⚠️ GitHub غير مربوط (GH_PAT)."))
        return
    ws = get_ws()
    try:
        repo = args[0].removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if "/" not in repo:
            repo = f"{await github_tools.login()}/{repo}"
        branch = args[1] if len(args) > 1 else "main"
        message = " ".join(args[2:]) or "Update from Telegram"
        res = await ws.push(repo, branch, message, github_tools.token())
        await send_html(context.bot, chat_id, lines_html(
            f"⬆️ <b>انرفع</b> على <code>{esc(repo)}</code> · 🌿 {esc(branch)} · <code>{res['commit']}</code>",
            f'<a href="{esc(res["url"])}">فتح على GitHub</a>' if res["url"].startswith("http") else ""))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, RLM + "⚠️ " + esc(str(e)))


# ----- أزرار الرد النهائي في وضع الوكلاء -----

async def on_agent_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    chat_id = q.message.chat_id
    kind, _, rest = q.data.partition(":")
    bot = context.bot
    if kind == "undo":
        await q.answer()
        await do_undo(bot, chat_id, rest)
    elif kind == "diff":
        if not (text := diffs.get(rest)):
            await q.answer("الفرق ما عاد محفوظ", show_alert=True)
            return
        await q.answer()
        await send_file(bot, chat_id, text.encode(), "changes.diff", "📄 فرق التعديلات")
    elif kind == "rep":
        if not (text := reports.get(rest)):
            await q.answer("التقارير ما عادت محفوظة", show_alert=True)
            return
        await q.answer()
        await send_file(bot, chat_id, text.encode(), "agents-reports.md", "🤖 تقارير الوكلاء")
    elif kind == "zip":
        await q.answer("📦 أجهّز الملف…")
        await send_zip(bot, chat_id)
    elif kind == "ask":
        key, _, idx = rest.partition(":")
        opts = asks.get(key)
        if not opts or not idx.isdigit() or int(idx) >= len(opts):
            await q.answer("السؤال ما عاد متاح — اكتب جوابك", show_alert=True)
            return
        if chat_id in active:
            await q.answer("استنى يخلص الرد الحالي")
            return
        await q.answer()
        asks.pop(key, None)
        answer = opts[int(idx)]
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(REGEN_KB)
        await send_html(bot, chat_id, RLM + f"↩️ <b>اخترت:</b> {esc(answer)}")
        await generate(bot, chat_id, Turn(None, answer, answer, {}, q.from_user.id, mode="agent",
                                          project=current_project()))
    elif kind == "plan":
        action, _, project = rest.partition(":")
        await q.answer()
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(REGEN_KB)
        if action != "go":
            await send_html(bot, chat_id, RLM + "تمام، ما نفّذت. اكتب ملاحظاتك على الخطة وأعدّلها.")
            return
        if chat_id in active:
            return
        await send_html(bot, chat_id, RLM + "✅ <b>وافقت على الخطة</b> — بدأ التنفيذ.")
        text = "✅ موافق على الخطة. نفّذها الحين بالكامل، وتحقق من النتيجة فعلياً."
        await generate(bot, chat_id, Turn(None, text, text, {"plan_mode": False}, q.from_user.id, mode="agent",
                                          project=project or current_project()))


# ----- متابعة CI على GitHub بالخلفية -----

watch_lock = asyncio.Lock()
watch_alerts: "OrderedDict[str, dict]" = OrderedDict()


def load_watches() -> dict:
    data = _read_json(dpath("watches.json"), {})
    return data if isinstance(data, dict) else {}


def digest_text(d: dict) -> str:
    parts = []
    for j in d.get("failed_jobs") or []:
        steps = "، ".join(j.get("failed_steps") or []) or "؟"
        parts.append(f"❌ {j.get('job')} ({j.get('conclusion')}) — الخطوات الفاشلة: {steps}\n{j.get('log', '')}")
    return "\n\n".join(parts) or "(ما لقيت تفاصيل الفشل)"


async def alert_failure(bot, repo: str, w: dict, r: dict) -> None:
    try:
        d = await github_tools.failure_digest(repo, r["id"])
    except Exception as e:
        d = {"failed_jobs": [], "error": str(e)}
    text = digest_text(d)
    key = new_key()
    _remember(watch_alerts, key, {"repo": repo, "run": r, "digest": text, "chat": w["chat"]}, keep=60)
    jobs = "، ".join(str(j.get("job")) for j in d.get("failed_jobs") or []) or "—"
    html = lines_html(f"🔴 <b>فشل CI</b> — <code>{esc(repo)}</code>",
                      f"⚙️ {esc(str(r.get('workflow')))} · 🌿 {esc(str(r.get('branch')))} · <code>{esc(str(r.get('sha')))}</code>",
                      f"❌ {esc(jobs)}")
    if r.get("url"):
        html += f'\n<a href="{esc(r["url"])}">فتح التشغيل على GitHub</a>'
    html += f"\n<blockquote expandable>{esc(text[:1500])}</blockquote>"
    kb = InlineKeyboardMarkup([[_btn("🛠 صلّحها الوكلاء", f"wfix:{key}"), _btn("🔁 أعد التشغيل", f"wre:{key}")],
                               [_btn("🔕 إيقاف المتابعة", f"woff:{key}")]])
    await send_html(bot, w["chat"], html, kb, keep=False)
    k = f"{r.get('workflow')}|{r.get('branch')}"
    streak = w.setdefault("autofix_streak", {})
    if w.get("auto") and r.get("head_sha") not in w.setdefault("fixed", []) and streak.get(k, 0) < 2:
        w["fixed"] = (w["fixed"] + [r.get("head_sha")])[-50:]
        streak[k] = streak.get(k, 0) + 1
        await start_ci_fix(bot, key)


async def start_ci_fix(bot, key: str) -> bool:
    a = watch_alerts.get(key)
    if not a:
        return False
    repo, r = a["repo"], a["run"]
    try:
        project = valid_name(repo.split("/")[-1])
    except WorkspaceError:
        project = "main"
    text = (f"🔴 فشل CI في المستودع {repo}\n"
            f"- workflow: {r.get('workflow')} · الفرع: {r.get('branch')} · commit: {r.get('sha')} · run_id: {r.get('id')}\n"
            f"- الرابط: {r.get('url')}\n\nتفاصيل الفشل:\n{a['digest'][:7000]}\n\n"
            f"المطلوب: صلّح سبب الفشل من جذره. لو المستودع مو مستنسخ في هذا المشروع استنسخه (git_clone) على الفرع "
            f"{r.get('branch')}. أعد إنتاج الخطأ محلياً بنفس أوامر الـ workflow، صلّحه وتحقق، ثم ارفع على نفس الفرع "
            "(git_push) وتابع CI (gh_actions action=watch) لين يصير أخضر. لا تعطّل اختبارات ولا تتجاهل الخطأ.")
    short = f"صلّح فشل CI في {repo} ({r.get('workflow')} على {r.get('branch')}، run {r.get('id')})"
    save_setting("project", project, "agent")
    get_ws(project)
    await send_html(bot, a["chat"], lines_html(f"🛠 <b>القائد بدأ يصلّح فشل CI</b> في <code>{esc(repo)}</code>",
                                               f"📁 المشروع: <code>{esc(project)}</code>"))
    asyncio.create_task(generate(bot, a["chat"], Turn(None, text, short, {}, OWNER_ID, mode="agent", project=project)))
    return True


async def watch_tick(bot) -> None:
    """يفحص المستودعات المتابَعة: أي تشغيل CI خلص بفشل → تنبيه (وإصلاح تلقائي لو مفعّل)."""
    data = load_watches()
    if not data or not github_tools.enabled():
        return
    changed = False
    for repo, w in list(data.items()):
        try:
            runs = await github_tools.recent_runs(repo, w.get("branch"), 15)
        except Exception as e:
            log.warning("watch %s failed: %s", repo, e)
            continue
        seen = set(w.get("seen") or [])
        primed = w.get("primed", False)
        failing = w.setdefault("failing", {})
        for r in sorted(runs, key=lambda r: r["id"]):
            if r.get("status") != "completed" or r["id"] in seen:
                continue
            seen.add(r["id"])
            changed = True
            if not primed:
                continue
            k = f"{r.get('workflow')}|{r.get('branch')}"
            if r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
                failing[k] = r["id"]
                await alert_failure(bot, repo, w, r)
            elif r.get("conclusion") == "success" and failing.pop(k, None):
                w.setdefault("autofix_streak", {}).pop(k, None)
                await send_html(bot, w["chat"], lines_html(
                    f"✅ <b>رجع CI أخضر</b> — <code>{esc(repo)}</code>",
                    f"⚙️ {esc(str(r.get('workflow')))} · 🌿 {esc(str(r.get('branch')))} · <code>{esc(str(r.get('sha')))}</code>"),
                    keep=False)
        if not primed:
            w["primed"], changed = True, True
        w["seen"] = sorted(seen)[-300:]
    if changed:
        _write_json(dpath("watches.json"), data)


async def watch_loop(bot) -> None:
    interval = float(os.getenv("WATCH_INTERVAL", "") or AGENT_CFG.get("watch_interval", 60))
    while True:
        await asyncio.sleep(interval)
        try:
            async with watch_lock:
                await watch_tick(bot)
        except Exception:
            log.warning("watch tick failed", exc_info=True)


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watch owner/repo [branch] [auto] · /watch off owner/repo · /watch (القائمة)."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    bot = context.bot
    if not github_tools.enabled():
        await send_html(bot, chat_id, RLM + "⚠️ GitHub غير مربوط — أضف السر GH_PAT.")
        return
    args = context.args or []
    async with watch_lock:
        data = load_watches()
        if not args:
            rows = ["👀 <b>متابعة CI</b>", ""]
            rows += [f"• <code>{esc(r)}</code>" + (f" · 🌿 {esc(w['branch'])}" if w.get("branch") else "")
                     + (" · 🛠 إصلاح تلقائي" if w.get("auto") else "") for r, w in data.items()] or ["ما فيه مستودعات متابَعة."]
            rows += ["", "إضافة: <code>/watch owner/repo [branch] [auto]</code>",
                     "<code>auto</code> = القائد يصلّح الفشل ويرفع لحاله · إيقاف: <code>/watch off owner/repo</code>"]
            await send_html(bot, chat_id, lines_html(*rows))
            return
        off = args[0].lower() in ("off", "stop", "ايقاف", "إيقاف")
        raw = args[1] if off and len(args) > 1 else args[0]
        repo = raw.removeprefix("https://github.com/").removesuffix(".git").strip("/")
        try:
            if "/" not in repo:
                repo = f"{await github_tools.login()}/{repo}"
        except Exception as e:
            await send_html(bot, chat_id, RLM + "⚠️ " + esc(str(e)[:200]))
            return
        if off:
            data.pop(repo, None)
            _write_json(dpath("watches.json"), data)
            await send_html(bot, chat_id, RLM + f"🔕 وقفت متابعة <code>{esc(repo)}</code>.")
            return
        extra = [a for a in args[1:]]
        auto = any(a.lower() in ("auto", "تلقائي") for a in extra)
        branch = next((a for a in extra if a.lower() not in ("auto", "تلقائي")), None)
        try:
            runs = await github_tools.recent_runs(repo, branch, 15)
        except Exception as e:
            await send_html(bot, chat_id, RLM + "⚠️ ما قدرت أقرأ CI للمستودع: " + esc(str(e)[:300]))
            return
        data[repo] = {"chat": chat_id, "branch": branch, "auto": auto, "primed": True,
                      "seen": sorted(r["id"] for r in runs if r.get("status") == "completed")}
        _write_json(dpath("watches.json"), data)
    last = runs[0] if runs else None
    icon = {"success": "✅", "failure": "❌", None: "⏳"}.get(last.get("conclusion") if last else None, "⚪️") if last else ""
    await send_html(bot, chat_id, lines_html(
        f"👀 <b>أتابع CI</b> في <code>{esc(repo)}</code>" + (f" · 🌿 {esc(branch)}" if branch else ""),
        f"آخر تشغيل: {icon} {esc(str(last.get('workflow')))} على {esc(str(last.get('branch')))}" if last else "ما فيه تشغيلات بعد.",
        "🛠 الإصلاح التلقائي شغّال: أي فشل، القائد يصلّحه ويرفع ويتابع لين يخضر." if auto
        else "أي فشل يوصلك تنبيه مع زر «🛠 صلّحها الوكلاء».",
    ))


async def on_watch_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("لصاحب البوت فقط", show_alert=True)
        return
    kind, _, key = q.data.partition(":")
    a = watch_alerts.get(key)
    if not a:
        await q.answer("التنبيه قديم — افتح /watch", show_alert=True)
        return
    if kind == "wfix":
        await q.answer("🛠 بدأ الإصلاح")
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(None)
        await start_ci_fix(context.bot, key)
    elif kind == "wre":
        try:
            await github_tools.rerun(a["repo"], a["run"]["id"])
            await q.answer("🔁 انعاد تشغيل الوظائف الفاشلة")
        except Exception as e:
            await q.answer(f"ما قدرت: {str(e)[:150]}", show_alert=True)
    elif kind == "woff":
        async with watch_lock:
            data = load_watches()
            data.pop(a["repo"], None)
            _write_json(dpath("watches.json"), data)
        await q.answer("🔕 وقفت المتابعة")
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(None)


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
        "settitle": cmd_settitle,
        "title": cmd_title,
        "snip": cmd_snip,
        "snippets": cmd_snippets,
        "delsnip": cmd_delsnip,
        "resetall": cmd_resetall,
        "agent": cmd_agent,
        "chat": cmd_chat,
        "plan": cmd_plan,
        "project": cmd_project,
        "files": cmd_files,
        "get": cmd_get,
        "zip": cmd_zip,
        "sh": cmd_sh,
        "undo": cmd_undo,
        "diff": cmd_diff,
        "memory": cmd_memory,
        "compact": cmd_compact,
        "cost": cmd_cost,
        "clone": cmd_clone,
        "push": cmd_push,
        "watch": cmd_watch,
        "set": cmd_set,
        "effort": shortcut("effort", lambda c: (c.args or ["default"])[0]),
        "thinking": shortcut("show_thinking", lambda c: "off" if cfg("show_thinking", owner_mode()) else "on"),
        "search": shortcut("web_search", lambda c: "off" if cfg("web_search", owner_mode()) else "on"),
    }


def _menu_commands(scope: str) -> list:
    """قائمة أوامر "/" حسب النطاق: default = للكل، owner = المالك بنسخة المحادثة، code = المالك بنسخة البرمجة."""
    want = {"default": {"both", "default"}, "owner": {"both", "default", "owner", "owner_all"},
            "code": {"both", "default", "code", "owner_all"}}[scope]
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
    await refresh_owner_menu(app.bot)


async def post_init(app: Application) -> None:
    await set_menu(app)
    app.bot_data["watch_task"] = asyncio.create_task(watch_loop(app.bot))  # متابعة CI بالخلفية


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("watch_task")
    if task:
        task.cancel()
    kill_all_jobs()  # أي خادم شغّله الوكلاء بالخلفية يوقف مع البوت


def build_app() -> Application:
    builder = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown)
               .concurrent_updates(True))
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
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(AGENT_BTN)}$"), cmd_agent))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CHAT_BTN)}$"), cmd_chat))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(PROJECT_BTN)}$"), cmd_project))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CODE_NEW_BTN)}$"), cmd_new))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CODE_SETTINGS_BTN)}$"), cmd_settings))

    app.add_handler(CallbackQueryHandler(on_stop, pattern=r"^stop$"))
    app.add_handler(CallbackQueryHandler(on_regen, pattern=r"^regen$"))
    app.add_handler(CallbackQueryHandler(on_full_thinking, pattern=r"^think:"))
    app.add_handler(CallbackQueryHandler(on_persona_button, pattern=r"^persona:"))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^(set:|cset:|noop$)"))
    app.add_handler(CallbackQueryHandler(on_agent_button, pattern=r"^(undo|diff|rep|ask|plan):|^zip$"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^(proj|projdel):"))
    app.add_handler(CallbackQueryHandler(on_watch_button, pattern=r"^(wfix|wre|woff):"))

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

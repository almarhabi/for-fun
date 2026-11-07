"""A Telegram bot with a fixed, owner-defined persona, running on DeepSeek V4.1 Flash (or GLM-5.3 Flash for bot 2), that behaves like modern AI apps — plus a Coding version with a lead model and parallel agents (like Claude Code)."""

import asyncio
import contextvars
import base64
import contextlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
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
import backends
import statestore
import config
import github_tools
import llm
import render
import telegraph
import vault
from llm import split_think_tags
from render import esc
from tools import TOOLS, describe_call, run_tool
from workspace import Changes, Checkpoints, Workspace, WorkspaceError, human, kill_all_jobs, valid_name

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
def clean_key(key: str) -> str:
    """Cleans common copy-paste mistakes from the key: spaces, quotes, the word Bearer."""
    key = key.strip().strip("\"'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


OPENROUTER_API_KEY = clean_key(os.environ["OPENROUTER_API_KEY"])
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# The model from config.json (OPENROUTER_MODEL can override it). Each model has one fixed provider.
DEFAULT_MODEL = config.DEFAULT_MODEL
OWNER_ID = int(os.getenv("OWNER_ID", "").strip() or "0")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))  # 10 messages per side

DATA_DIR = Path(__file__).parent / "data"
PERSONA_FILE = DATA_DIR / "persona.json"
RULES_FILE = DATA_DIR / "rules.json"       # lock rules edited from Telegram
META_FILE = DATA_DIR / "meta.json"          # X-Title (encrypted)
SNIPPETS_FILE = DATA_DIR / "snippets.json"  # the owner's snippets (saved text/files)
HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SCREEN_FILE = DATA_DIR / "screen.json"
TELEGRAPH_FILE = DATA_DIR / "telegraph.json"
# Coding-mode files — resolved at use time (tests may change DATA_DIR)


def dpath(name: str) -> Path:
    return DATA_DIR / name


def ws_base() -> Path:
    return DATA_DIR / "workspaces"


def checkpoints() -> Checkpoints:
    return Checkpoints(dpath("checkpoints.json"))

MAX_TOOL_ROUNDS = 10     # enough for GitHub tasks (read, edit, several files, PR)
EDIT_INTERVAL = 1.0      # Telegram allows editing a message roughly once per second
GROUP_WAIT = 1.2         # wait for the remaining parts (a photo album or a long message Telegram split)
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_CHARS = 60_000
SINGLE_MAX = 3900        # replies longer than this (HTML chars) are published as a tidy Telegra.ph page

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("persona-bot")
for noisy in ("httpx", "primp", "ddgs"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# X-Title isn't written here: it's set from Telegram (/settitle), stored encrypted, and sent with every request
openrouter_client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
# 🆓 NVIDIA NIM: several keys, the load spread over them (backends.KeyPool)
nvidia_pool = backends.KeyPool(config.nvidia_keys(), config.NVIDIA["base_url"],
                               int(config.NVIDIA.get("rpm_per_key", 40)), float(config.NVIDIA.get("max_wait_seconds", 45)))
# Every request (chat, the coding lead, the agents) goes through the router to the backend the owner picked
client = backends.ModelRouter(openrouter_client, nvidia_pool, lambda: cfg("backend"), config.NVIDIA_MODELS,
                              config.NVIDIA.get("model_extra") or {})
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
PREVIEW = LinkPreviewOptions(is_disabled=False, prefer_large_media=False)
tph = telegraph.Telegraph(TELEGRAPH_FILE, short_name="PersonaBot")


# ================= Storage =================

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# A durable copy of the important files (persona, rules, identities, settings…) outside the Actions cache
state_store = statestore.StateStore()


def _write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    text = json.dumps(data, ensure_ascii=False)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    if path.parent == DATA_DIR:
        state_store.schedule(path.name, text)


# Conversation memory per chat
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
# Coding memory: separate per (chat, project) and short — older context lives in the compact project journal
AGENT_CFG = config.AGENT
AGENT_HISTORY = int(AGENT_CFG.get("history_messages", 8))
agent_histories: dict[str, deque] = defaultdict(lambda: deque(maxlen=AGENT_HISTORY))
# Ids of the messages shown in each chat (so a new chat can clear the screen)
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


# ================= Persona and rules =================

# Persona/style rules and the GitHub note — all in config.json (edit them there)
LOCK_RULES = config.LOCK_RULES
STYLE_RULES = config.STYLE_RULES
GITHUB_NOTE = config.GITHUB_NOTE
CONFIDENTIAL_NOTE = config.CONFIDENTIAL_NOTE
DEFAULT_PERSONA = config.DEFAULT_PERSONA


def _load_secret(path: Path, key: str, default: str) -> str:
    """Reads an encrypted value and decrypts it. If it's encrypted and we lack the key, returns the default with a warning."""
    data = _read_json(path, {})
    val = data.get(key) if isinstance(data, dict) else None
    if val is None:
        return default
    dec = vault.decrypt(val)
    if dec is None:
        log.warning("Could not decrypt %s (PERSONA_KEY missing or wrong) — using the default", key)
        return default
    return dec


def load_persona() -> str:
    return _load_secret(PERSONA_FILE, "persona", DEFAULT_PERSONA)


def save_persona(text: str) -> None:
    _write_json(PERSONA_FILE, {"persona": vault.encrypt(text)})


def load_lock() -> str:
    """The current lock rules — the ones edited from Telegram if any, otherwise from config.json."""
    return _load_secret(RULES_FILE, "lock", config.LOCK_RULES)


def save_lock(text: str) -> None:
    _write_json(RULES_FILE, {"lock": vault.encrypt(text)})


# The Coding version's own identities: the lead's and the agents' (encrypted like the persona). Empty = not set.
IDENTITY_FILES = {"lead": "lead_persona.json", "agents": "agents_persona.json"}


def load_identity(who: str) -> str:
    return _load_secret(dpath(IDENTITY_FILES[who]), "persona", "")


def save_identity(who: str, text: str) -> None:
    if text.strip():
        _write_json(dpath(IDENTITY_FILES[who]), {"persona": vault.encrypt(text)})
    else:
        dpath(IDENTITY_FILES[who]).unlink(missing_ok=True)


def lead_identity() -> tuple[str, str]:
    """(identity, rules) for the lead: its own identity if set, otherwise the Chat persona as a voice only.
    Both need Persona voice on."""
    if not (cfg("persona_voice", "agent") and AGENT_CFG.get("persona_voice", True)):
        return "", ""
    own = load_identity("lead")
    return (own, config.LEAD_IDENTITY_RULES) if own.strip() else (load_persona(), "")


def load_title() -> str:
    """The X-Title sent to OpenRouter — empty = not sent."""
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
    # For non-owners: block revealing the instructions/persona. The owner sees them via /persona and /rules
    head = (CONFIDENTIAL_NOTE + "\n") if confidential and CONFIDENTIAL_NOTE else ""
    return f"{head}{load_lock()}\n{STYLE_RULES}{extra}\n<identity>\n{load_persona()}\n</identity>"


# ================= Settings =================

# Reasoning levels, settings and their defaults — all in config.json
EFFORTS = config.EFFORTS
EFFORT_API = config.EFFORT_API
PARAMS = config.PARAMS
BOOL_KEYS = config.BOOL_KEYS
DEFAULTS = config.DEFAULTS


CODE_DEFAULTS = config.CODE_DEFAULTS
GLOBAL_KEYS = config.GLOBAL_KEYS


def cfg(key: str, mode: str = "chat"):
    """A setting value. The bot has two versions: mode="chat" (💬) or "agent" (💻 Coding) — each has its own settings."""
    data = _read_json(SETTINGS_FILE, {})
    if mode == "agent" and key not in GLOBAL_KEYS:
        code = data.get("code") if isinstance(data.get("code"), dict) else {}
        value = code.get(key, CODE_DEFAULTS.get(key))
    else:
        value = data.get(key, DEFAULTS.get(key))
    if key in ("model", "worker_model"):
        if key == "model" and "/" not in str(value):  # an old direct-Moonshot model name
            return DEFAULT_MODEL
        return config.current_model(value) or DEFAULTS.get(key)  # an old model name moves to its replacement
    return value


def backend_name() -> str:
    return "nvidia" if cfg("backend") == "nvidia" else "openrouter"


def backend_label() -> str:
    if backend_name() == "nvidia":
        return f"NVIDIA (free · {len(nvidia_pool)} key{'s' if len(nvidia_pool) != 1 else ''})"
    return "OpenRouter (paid)"


def provider_text(model) -> str:
    """Where a model runs right now, for display: its fixed OpenRouter provider, or NVIDIA."""
    if backend_name() == "nvidia":
        return f"NVIDIA {config.NVIDIA_MODELS.get(config.current_model(model) or '', model)}"
    prov = config.provider_for(model)
    return ", ".join(prov["only"]) if prov else "auto"


def save_setting(key: str, value, mode: str = "chat") -> None:
    data = _read_json(SETTINGS_FILE, {})
    if mode == "agent" and key not in GLOBAL_KEYS:
        data.setdefault("code", {})[key] = value
    else:
        data[key] = value
    _write_json(SETTINGS_FILE, data)


def owner_mode() -> str:
    """The version the owner currently has open."""
    return "agent" if cfg("agent_mode") else "chat"


def parse_value(key: str, raw: str):
    typ = PARAMS[key][0]
    raw = raw.strip()
    low = raw.lower()
    if typ is bool:
        if low in ("on", "true", "1", "yes"):
            return True
        if low in ("off", "false", "0", "no"):
            return False
        raise ValueError("use on or off")
    if low in ("off", "none", "default"):
        if key == "effort":
            return "default"
        return DEFAULTS[key] if key in ("model", "worker_model", "max_agents") else None
    if key == "backend":
        if low in ("nvidia", "free", "nim"):
            return "nvidia"
        if low in ("openrouter", "paid", "or"):
            return "openrouter"
        raise ValueError("values: openrouter (paid) / nvidia (free)")
    if key == "permissions":
        if low not in ("auto", "ask"):
            raise ValueError("values: auto / ask")
        return low
    if key == "effort":
        if low not in EFFORTS:
            raise ValueError("values: " + " / ".join(EFFORTS))
        return low
    try:
        return typ(raw)
    except ValueError:
        raise ValueError(f"must be a number ({typ.__name__})") from None


def request_kwargs(overrides: dict | None = None, mode: str = "chat") -> dict:
    o = overrides or {}

    def get(k):
        return o.get(k, cfg(k, mode))

    effort = get("effort")
    extra = {"reasoning": {"enabled": True} if effort == "default" else {"effort": EFFORT_API.get(effort, effort)}}
    model = config.current_model(get("model")) or DEFAULT_MODEL
    if provider := config.provider_for(model):   # always the model's own provider, never a fallback
        extra["provider"] = provider
    kw = {"model": model, "extra_body": extra}
    for k in ("temperature", "top_p", "max_tokens"):
        if get(k) is not None:
            kw[k] = get(k)
    return kw


def split_overrides(text: str) -> tuple[dict, str]:
    """Settings for a single message, at its start, e.g.: effort=low temperature=0.3 your question"""
    overrides, words = {}, text.split(" ")
    while words and "=" in words[0]:
        k, _, v = words[0].partition("=")
        if k not in PARAMS:
            break
        overrides[k] = parse_value(k, v)
        words.pop(0)
    return overrides, " ".join(words).strip()


# ================= Telegram helpers =================

NEW_CHAT_BTN = config.NEW_CHAT_BTN
SETTINGS_BTN = config.SETTINGS_BTN
AGENT_BTN = config.AGENT_BTN
CHAT_BTN = config.CHAT_BTN
PROJECT_BTN = config.PROJECT_BTN
CODE_NEW_BTN = config.CODE_NEW_BTN
CODE_SETTINGS_BTN = config.CODE_SETTINGS_BTN
MENU_BTN = config.MENU_BTN
BACK_BTN = config.BACK_BTN
STOP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Stop", callback_data="stop")]])
REGEN_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Regenerate", callback_data="regen")]])


def is_owner_id(user_id: int | None) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and is_owner_id(update.effective_user.id)


def main_keyboard(owner: bool) -> ReplyKeyboardMarkup:
    if not owner:
        return ReplyKeyboardMarkup([[NEW_CHAT_BTN]], resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder="Type your message…")
    if cfg("agent_mode"):  # the Coding version
        return ReplyKeyboardMarkup([[CODE_NEW_BTN, PROJECT_BTN, MENU_BTN], [CODE_SETTINGS_BTN, CHAT_BTN]],
                                   resize_keyboard=True, is_persistent=True,
                                   input_field_placeholder="Ask for a project or a coding task…")
    return ReplyKeyboardMarkup([[NEW_CHAT_BTN, SETTINGS_BTN], [AGENT_BTN, MENU_BTN]], resize_keyboard=True,
                               is_persistent=True, input_field_placeholder="Type your message…")


def lines_html(*lines: str) -> str:
    """Joins the bot's message lines (lists and notices)."""
    return "\n".join(lines)


async def send_html(bot, chat_id: int, text: str, markup=None, keep: bool = True) -> Message | None:
    """Sends an HTML message; if the formatting fails, sends plain text. keep=True tracks it so a new chat clears it."""
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
            for mid in batch[-30:]:  # if the batch failed, try the latest messages one by one
                with contextlib.suppress(TelegramError):
                    await bot.delete_message(chat_id, mid)


def should_respond(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """In private chats it answers everything; in groups only when mentioned or replied to."""
    msg = update.message
    if not msg or update.effective_chat.type == ChatType.PRIVATE:
        return True
    username = (context.bot.username or "").lower()
    text = (msg.text or msg.caption or "").lower()
    replied = msg.reply_to_message and msg.reply_to_message.from_user and \
        msg.reply_to_message.from_user.id == context.bot.id
    return bool(replied or (username and f"@{username}" in text))


# ================= Live reply display =================

class Panel:
    """A group of Telegram messages that get updated (the thinking message, or the reply messages)."""

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
                    # if Telegram rejects the formatting, send plain text instead of losing the reply
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


think_store: "OrderedDict[str, str]" = OrderedDict()  # the full thinking, for the "Full thinking" button


def remember_thinking(text: str) -> str:
    key = str(int(time.time() * 1000))[-9:]
    think_store[key] = text
    while len(think_store) > 100:
        think_store.popitem(last=False)
    return key


class ResponseUI:
    """Shows one reply: a thinking message that updates first, then the reply written live in front of the user."""

    def __init__(self, bot, chat_id: int, show_thinking: bool, long_pages: bool = True,
                 board: "agent.Board | None" = None):
        self.bot, self.chat_id, self.show_thinking = bot, chat_id, show_thinking
        self.long_pages = long_pages
        self.board = board                     # coding mode: the work panel (plan + agents)
        self.think = Panel(bot, chat_id)
        self.work = Panel(bot, chat_id)        # between the thinking and the reply, in a fixed order
        self.reply = Panel(bot, chat_id)
        self.extra_rows: list[list[InlineKeyboardButton]] = []  # buttons above on the final reply
        self.inbox: list[str] = []             # the owner's messages during a run (steering)
        self.final_files: list[dict] = []
        self.allow_all = False                 # permission mode "ask": owner tapped "Allow all"
        self.parts: list[list[str]] = []      # [["r", reasoning], ["s", a step] or ["n", narration]]
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

    # ----- State updates (non-blocking) -----
    def begin_round(self) -> None:
        self._round_part = None
        if self.board is not None:  # coding mode: the reply = the last round's text only (earlier text moves to the log)
            self.reply_md = ""
        self._reply_base = self.reply_md

    def demote(self, text: str) -> None:
        """The lead's text before calling tools: moves from the reply area to the steps log."""
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
        # the reply accumulates across rounds: text written before a search stays, new text is appended
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

    # ----- Rendering -----
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
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Full thinking", callback_data=f"think:{self._think_key}")]])
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
        return InlineKeyboardMarkup(self.extra_rows + [[InlineKeyboardButton("Regenerate", callback_data="regen")]])

    def _final_extras(self) -> list[str]:
        extras = []
        if self.stopped:
            extras.append("<i>Reply stopped</i>")
        if self.error:
            extras.append(self.error)
        return extras

    def live_pages(self) -> list:
        """Live preview: one message updated while writing. No splitting or page publishing until the end."""
        if not self._reply_active():
            return []
        h = render.live_tail_html(self.reply_md) if self.reply_md.strip() else ""
        if not h and self.status:
            h = f"<i>{esc(self.status)}</i>"
        h = f"{h} {render.CURSOR}" if h else render.CURSOR
        return [(h, STOP_KB, False)]

    async def final_reply_pages(self) -> list:
        """Delivers the final reply: one message if short, a Telegra.ph page if long, and splitting as a fallback."""
        md = self.reply_md.strip("\n")
        extras = self._final_extras()
        src_html = render.sources_html(self.sources) if self.sources else ""
        if not md:
            body = "\n\n".join(extras) if extras else "<i>…</i>"
            return [(body, self.final_kb(), False)]
        full = render.render(md, partial=False)  # full, uncut; the decision uses the real length
        tail = ("\n\n" + "\n\n".join(extras)) if extras else ""
        if len(full) + len(src_html) + len(tail) <= SINGLE_MAX:  # one message is enough
            body = full + (("\n\n" + src_html) if src_html else "") + tail
            return [(body, self.final_kb(), False)]
        if self.long_pages and not self.stopped:  # a long reply → a page that opens inside Telegram
            try:
                nodes = render.to_telegraph_nodes(md)
                if self.sources:
                    nodes.append({"tag": "hr"})
                    nodes.append({"tag": "h3", "children": ["Sources"]})
                    nodes.append({"tag": "ul", "children": [
                        {"tag": "li", "children": [{"tag": "a", "attrs": {"href": s["url"]},
                                                    "children": [s.get("title") or s["url"]]}]}
                        for s in self.sources if s.get("url")]})
                url = await tph.create_page(render.telegraph_title(md), nodes)
                head = lines_html("<b>Full reply</b> — it's long, so I put it on a tidy page that opens inside Telegram:",
                                  f'<a href="{esc(url)}">Open the full reply</a>')
                return [(head + tail, self.final_kb(), True)]
            except Exception:
                log.warning("telegraph failed; splitting instead", exc_info=True)
        chunks = render.split_md(md)  # fallback: split — guarantees nothing is lost
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
        interval = EDIT_INTERVAL * (2 if self.board is not None else 1)  # 3 messages updating → gentler on Telegram's limits
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() - last_typing > 4.5:
                last_typing = time.monotonic()
                with contextlib.suppress(TelegramError):
                    await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
            await self.flush()

    async def start(self) -> None:
        await self.flush()  # shows "Thinking…" right away
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


# ================= Generation =================

@dataclass
class Turn:
    """The user's message and the bot's reply to it (for regenerate and message edits)."""
    user_msg_id: int | None
    content: object          # what is sent to the model (text, or text + images)
    history_text: str        # what is saved in memory
    overrides: dict
    user_id: int | None
    response_ids: list[int] = field(default_factory=list)
    mode: str = "chat"               # chat | agent
    project: str = ""                # coding mode: the project name
    checkpoint: str | None = None    # the checkpoint for this request's changes


def turn_history(chat_id: int, turn: Turn) -> deque:
    return agent_histories[agent_key(chat_id, turn.project)] if turn.mode == "agent" else histories[chat_id]


def save_turn_history(turn: Turn) -> None:
    (save_agent_histories if turn.mode == "agent" else save_histories)()


last_turn: dict[int, Turn] = {}
no_tools: set[str] = set()  # model/provider pairs that don't support tools


def tools_key(kw: dict) -> str:
    return kw["model"] + "|" + json.dumps(kw.get("extra_body", {}).get("provider"), sort_keys=True)


def keep_recent_reasoning(history: deque, keep: int = 10) -> None:
    """Keep reasoning_details on the last 10 replies (all replies in memory)."""
    seen = 0
    for msg in reversed(history):
        if msg.get("role") == "assistant" and "reasoning_details" in msg:
            seen += 1
            if seen > keep:
                msg.pop("reasoning_details")
active: dict[int, ResponseUI] = {}
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def stream_round(ui: ResponseUI, messages: list, kw: dict, tool_kwargs: dict):
    """One round with the model. Returns (reply text, reasoning, reasoning details, tool calls)."""
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
    if isinstance(e, backends.NoKeyAvailable):
        return str(e) + " /doctor checks every key."
    code = getattr(e, "status_code", None)
    msg = str(e).lower()
    if code == 401:
        return ("OpenRouter rejected the key. Make sure the OPENROUTER_API_KEY secret holds the full key "
                "(it starts with sk-or-v1-) from openrouter.ai/keys, and that the key wasn't deleted.")
    if code == 402 or "credit" in msg:
        return "Not enough OpenRouter credit."
    if code == 429:
        return "The provider is overloaded — try again in a moment."
    if "tool" in msg and ("support" in msg or "endpoint" in msg):
        return "This model's provider doesn't accept tools right now (Coding mode needs them) — try again in a moment."
    if code == 404 or "no endpoints" in msg or "provider" in msg:
        return "This model's provider isn't available right now — try again in a moment."
    return ""


def error_html(e: Exception, owner: bool) -> str:
    text = "⚠️ <b>An error occurred and I couldn't finish the reply.</b>\nTap Regenerate to try again."
    if owner and (hint := error_hint(e)):
        text += "\n" + esc(hint)
    if owner:
        text += f"\n<blockquote expandable>{esc(type(e).__name__)}: {esc(str(e)[:700])}</blockquote>"
    return text


regen_msg: dict[int, int] = {}  # the message carrying the button in each chat


async def drop_old_buttons(bot, chat_id: int) -> None:
    """The regenerate button stays on the latest reply only."""
    mid = regen_msg.pop(chat_id, None)
    if mid:
        with contextlib.suppress(TelegramError):
            await bot.edit_message_reply_markup(chat_id, mid, reply_markup=None)


async def send_transcript(bot, chat_id: int, turn: "Turn", ui: "ResponseUI") -> int | None:
    """A txt file after the reply: the persona, the user's message, the full thinking, and the reply. Owner only."""
    text = (
        "Persona:\n" + load_persona() + "\n\n"
        "My message:\n" + (turn.history_text or "") + "\n\n"
        "Full thinking:\n" + (ui._full_thinking().strip() or "—") + "\n\n"
        "Reply:\n" + (ui.reply_md.strip() or "—") + "\n"
    )
    try:
        m = await bot.send_document(chat_id, InputFile(io.BytesIO(text.encode("utf-8")), filename="reply.txt"),
                                    caption="Reply transcript")
        return m.message_id
    except TelegramError as e:
        log.warning("transcript failed: %s", e)
        return None


def active_tools(owner: bool, use_search: bool) -> list:
    """Search tools for everyone, GitHub tools for the owner only (and only if the token is set)."""
    tools = list(TOOLS) if use_search else []
    if owner and github_tools.enabled():
        tools += github_tools.GITHUB_TOOLS
    return tools


def describe_tool(name: str, args: str) -> str:
    if github_tools.is_github_tool(name):
        return github_tools.describe_github(name, args)
    return describe_call(name, args)


async def dispatch_tool(name: str, args: str, owner: bool):
    """Runs the tool. GitHub tools are owner-only — a second check even if one is requested by mistake."""
    if github_tools.is_github_tool(name):
        if not owner:
            return "This tool is for the bot owner only.", []
        return await github_tools.run_github(name, args), []
    return await run_tool(name, args)


async def _generate(bot, chat_id: int, turn: Turn) -> None:
    """Generates a full reply (the chat lock must be held)."""
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
            else:  # out of rounds — must answer without more tools
                tool_kwargs = {"tools": tools_list, "tool_choice": "none"}
            ui.begin_round()
            try:
                content, reasoning, details, calls = await stream_round(ui, messages, kw, tool_kwargs)
            except APIError as e:
                # some providers don't support tools: continue without them instead of failing the reply
                if not tool_kwargs or "tool" not in str(e).lower():
                    raise
                no_tools.add(tools_key(kw))
                tools_list, tool_kwargs = [], {}
                ui.add_step("⚠️ Tools aren't supported by this provider — continued without them")
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
                assistant["reasoning_details"] = details  # send it back as-is so it continues its reasoning
            messages.append(assistant)
            for c in calls:
                ui.add_step(describe_tool(c["name"], c["args"]))
                result, sources = await dispatch_tool(c["name"], c["args"], owner)
                ui.add_sources(sources)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    except asyncio.CancelledError:
        if not ui.stopped:
            raise
        asyncio.current_task().uncancel()  # the user tapped — keep what was written
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
            entry["reasoning_details"] = final_details  # it continues reasoning where it left off in the next message
        history.append(entry)
        keep_recent_reasoning(history)
        save_histories()

    # txt file after the reply — owner only, so the persona never reaches anyone else
    if owner and overrides.get("export_txt", cfg("export_txt")) and (reply or ui.parts):
        doc_id = await send_transcript(bot, chat_id, turn, ui)
        if doc_id:
            turn.response_ids.insert(0, doc_id)  # deleted with the reply on , and the button stays on the reply
            track(chat_id, doc_id)


# ================= Coding mode =================

session_usage = agent.Usage()          # usage since this process started
diffs: "OrderedDict[str, str]" = OrderedDict()      # each request's diff (the Diff button)
reports: "OrderedDict[str, str]" = OrderedDict()    # the agents' reports (the Reports button)
asks: "OrderedDict[str, list[str]]" = OrderedDict()  # options of the lead's questions (buttons)


def _remember(store: OrderedDict, key: str, value, keep: int = 40) -> None:
    store[key] = value
    while len(store) > keep:
        store.popitem(last=False)


def new_key() -> str:
    return f"{int(time.time() * 1000) % 10**10}"


def record_usage(models: dict) -> None:
    """Adds up usage (tokens and cost) per model: for this session and the saved all-time total."""
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
    """The project journal: two lines per request — gives the lead the context of everything that happened without resending old conversations in full."""
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
    for e in reversed(entries):  # the newest matters most; if the journal gets long, the oldest is summarized away
        files = f" | files: {' '.join(e['files'])}" if e.get("files") else ""
        line = f"- [{e['t']}] {'' if e.get('ok', True) else '⚠️ '}request: {e['req']} → {e['out']}{files}"
        if used + len(line) > limit:
            lines.append(f"- … and {len(entries) - len(lines)} older requests")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def restore_checkpoint(project: str, cp_id: str | None = None) -> tuple[int, list[str]] | None:
    """Restores the project files to how they were before the latest request (if cp_id is the latest)."""
    store = checkpoints()
    cp = store.pop(project, cp_id)
    if not cp:
        return None
    return Changes.from_json(get_ws(project), cp["files"]).restore()


def lead_kwargs(overrides: dict) -> dict:
    """The lead's request: same shape as chat but with the Coding version's settings (reasoning high by default) + usage accounting."""
    kw = request_kwargs(overrides, "agent")
    kw["extra_body"]["usage"] = {"include": True}
    return kw


def worker_kwargs() -> dict:
    extra = {"reasoning": {"enabled": True}, "usage": {"include": True}}
    model = cfg("worker_model", "agent") or config.WORKER_MODEL
    if provider := config.provider_for(model):
        extra["provider"] = provider
    return {"model": model, "extra_body": extra}


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
        preload_chars=int(AGENT_CFG.get("preload_chars", 60_000)),
        compact_chars=int(AGENT_CFG.get("compact_stale_chars", 30_000)),
        max_timeout=int(AGENT_CFG["max_command_timeout"]),
        web=(lambda n, a: run_tool(n, a)) if web_on else None, web_tools=list(TOOLS) if web_on else [],
        gh=(lambda n, a, progress=None: github_tools.run_github(n, a, progress)) if gh_on else None,
        gh_token=github_tools.token(), gh_login=github_tools.login if gh_on else None,
        headers={"X-Title": t} if (t := load_title()) else {}, worker_prompt=config.WORKER_PROMPT,
        agent_persona=load_identity("agents"), agent_identity_rules=config.AGENT_IDENTITY_RULES)


async def lead_round(ui: ResponseUI, messages: list, kw: dict, tool_kwargs: dict):
    """A lead round with retries when the provider is overloaded (long work isn't lost to a transient error)."""
    for attempt in range(3):
        try:
            return await stream_round(ui, messages, kw, tool_kwargs)
        except Exception as e:
            if attempt == 2 or not agent._transient(e):
                raise
            ui.add_step(f"Provider busy — retry {attempt + 1}")
            ui.begin_round()
            await asyncio.sleep(4 * (attempt + 1) ** 2)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


async def _generate_agent(bot, chat_id: int, turn: Turn) -> None:
    """Coding mode: the lead plans, reads, writes, runs, delegates to the agents and reviews; the reply summarizes the work."""
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
    persona, identity_rules = lead_identity()
    system = agent.lead_system(env, config.LEAD_PROMPT, plan, persona or None, GITHUB_NOTE, journal_text(ws.name),
                               identity_rules)
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": turn.content}]
    await drop_old_buttons(bot, chat_id)

    ui = ResponseUI(bot, chat_id, show_thinking, long_pages, board=board)
    env.on_status = lambda text: setattr(ui, "status", text)
    if cfg("permissions", "agent") == "ask":
        env.approve = make_approver(bot, chat_id, ui)
    ui.task = asyncio.current_task()
    active[chat_id] = ui
    ctx = agent.ToolCtx(env)
    terminal: tuple[str, dict, str] | None = None
    last_text = ""
    max_rounds = int(AGENT_CFG["lead_rounds"])
    try:
        await ui.start()
        for round_no in range(max_rounds + 1):
            if ui.inbox:  # the owner sent steering while it works → it reaches the lead right away
                note = "\n\n".join(ui.inbox)
                ui.inbox.clear()
                messages.append({"role": "user", "content": f"[Message from the owner during the run]\n{note}"})
                turn.history_text += f"\n\n[During the run]: {note}"
                ui.add_step("Your note: " + " ".join(note.split())[:80])
            if agent.trim_context(messages):
                ctx.seen_reads.clear()
            else:
                agent.compact_stale(messages, env.compact_chars)
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

            async def one(c: dict):
                board.tool_calls += 1
                ui.add_step(agent.describe(c["name"], c["args"]) or describe_tool(c["name"], c["args"]))
                if c["name"] in agent.TERMINAL:
                    return "Shown to the owner.", []
                return await agent.execute_lead(env, ctx, c["name"], c["args"])

            # independent read-only calls in one reply run at the same time (fewer, shorter rounds)
            outs = await agent.run_batch(calls, one)
            for c, (result, sources) in zip(calls, outs):
                if c["name"] in agent.TERMINAL and not terminal:
                    terminal = (c["name"], llm.parse_args(c["args"]) or {}, content)
                ui.add_sources(sources)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            ui.status = ""
            if terminal:
                break
    except asyncio.CancelledError:
        if not ui.stopped:
            raise
        asyncio.current_task().uncancel()  # the owner tapped — keep what happened
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
            q = str(args.get("question") or "").strip() or "I have a question before I continue:"
            opts = [" ".join(str(o).split())[:60] for o in (args.get("options") or []) if str(o).strip()][:5]
            ui.reply_md = lead_in + "" + q
            if opts:
                _remember(asks, key, opts)
                ui.extra_rows += [[_btn(o, f"ask:{key}:{i}")] for i, o in enumerate(opts)]
                ui.reply_md += "\n\n_Pick a button or type your answer._"
        else:
            ui.reply_md = lead_in + (str(args.get("plan") or "").strip() or "(empty plan)")
            ui.extra_rows.append([_btn("Run the plan", f"plan:go:{ws.name}"[:64]), _btn("No", "plan:no")])
    elif not ui.reply_md.strip() and last_text and not ui.error:
        ui.reply_md = last_text  # the lead finished without a final reply — show its last text instead of an empty reply

    files = changes.files()
    ui.final_files = files
    if files:
        checkpoints().push(ws.name, key, turn.history_text, changes)
        turn.checkpoint = key
        _remember(diffs, key, changes.diff())
        ui.extra_rows.append([_btn("Undo", f"undo:{key}"), _btn("Diff", f"diff:{key}"),
                              _btn("Project", "zip")])
    if board.agents:
        _remember(reports, key, agent.reports_markdown(board))
        ui.extra_rows.append([_btn(f"Agent reports ({len(board.agents)})", f"rep:{key}")])

    await ui.finish()
    turn.response_ids = ui.message_ids()
    last_turn[chat_id] = turn
    if turn.response_ids:
        regen_msg[chat_id] = turn.response_ids[-1]
    track(chat_id, *turn.response_ids)
    record_usage(board.usage.models)
    last_boards[chat_id] = board
    reply = ui.reply_md.strip()
    if reply and not ui.error:
        history.append({"role": "user", "content": turn.history_text})
        log_text = agent.work_log(board, files)
        history.append({"role": "assistant", "content": reply + ("\n\n" + log_text if log_text else "")})
        save_agent_histories()
    if reply or files:
        journal_add(ws.name, turn.history_text, reply or "(no reply)", files, ok=not (ui.error or ui.stopped))

    if overrides.get("export_txt", cfg("export_txt", "agent")) and (reply or ui.parts):
        doc_id = await send_transcript(bot, chat_id, turn, ui)
        if doc_id:
            turn.response_ids.insert(0, doc_id)
            track(chat_id, doc_id)
    if ui.inbox:  # steering that arrived after the last round → a new request right after this one
        note = "\n\n".join(ui.inbox)
        asyncio.create_task(generate(bot, chat_id, Turn(None, note, note, {}, turn.user_id,
                                                         mode="agent", project=ws.name)))


async def run_turn(bot, chat_id: int, turn: Turn) -> None:
    """The chat lock must be held."""
    if turn.mode == "agent":
        await _generate_agent(bot, chat_id, turn)
    else:
        await _generate(bot, chat_id, turn)


async def generate(bot, chat_id: int, turn: Turn) -> None:
    async with chat_locks[chat_id]:  # new messages wait their turn
        await run_turn(bot, chat_id, turn)


async def redo(bot, chat_id: int, user_msg_id: int | None, replacement: Turn | None = None) -> bool:
    """Regenerates the latest reply (🔄) or answers an edited message again."""
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
        if turn.mode == "agent" and turn.checkpoint:  # in coding mode = restore the files and redo from the same point
            restore_checkpoint(turn.project, turn.checkpoint)
        await delete_many(bot, chat_id, turn.response_ids)
        untrack(chat_id, turn.response_ids)
        last_turn.pop(chat_id, None)
        new = replacement or Turn(turn.user_msg_id, turn.content, turn.history_text, turn.overrides, turn.user_id)
        new.mode, new.project = turn.mode, turn.project
        await run_turn(bot, chat_id, new)
        return True


# ================= Receiving user messages =================

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
    notes: list[str] = field(default_factory=list)   # coding mode: files saved into the project


async def download(bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    return bytes(await f.download_as_bytearray())


async def build_item(msg: Message, bot, ws: Workspace | None = None) -> Item | str:
    """Turns the user's message into content. Returns an error string if the type isn't supported.
    ws (coding mode, owner): files are saved into the project — any type, and zips are extracted."""
    item = Item(msg=msg, text=msg.text or msg.caption or "")
    if ws is not None and msg.document:
        doc = msg.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            return "The file is too big — Telegram's limit for bots is 20 MB."
        data = await download(bot, doc.file_id)
        try:
            item.notes.append(await asyncio.to_thread(ws.save_upload, doc.file_name or "file", data))
        except WorkspaceError as e:
            return str(e)
        name = doc.file_name or "file"
        if (doc.mime_type or "").startswith("image/"):
            item.images.append(f"data:{doc.mime_type};base64," + base64.b64encode(data).decode())
        elif not name.lower().endswith(".zip") and (text := decode_text(data)) is not None and len(text) <= 6000:
            item.files.append((name, text))  # a small file: send it directly and save a read round
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
            return "The file is too big — the limit is 10 MB."
        name = doc.file_name or "file"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = doc.mime_type or ""
        data = await download(bot, doc.file_id)
        if mime.startswith("image/"):
            item.images.append(f"data:{mime};base64," + base64.b64encode(data).decode())
        elif ext == "pdf" or mime == "application/pdf":
            text = await asyncio.to_thread(pdf_text, data)
            if not text.strip():
                return "This PDF has no text I can read (probably scanned images)."
            item.files.append((name, text))
        else:
            text = decode_text(data) if (ext in TEXT_EXT or mime.startswith("text/") or not ext) else None
            if text is None:
                return f"I can't read .{ext or '?'} files — send text, an image, a PDF or a text file."
            item.files.append((name, text))
    elif not item.text:
        return "I can't handle this kind of message. Send text, an image or a file."
    return item


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages[:80])


def quote_context(msg: Message) -> str:
    """If the user replied to a message, give the model the part they replied to."""
    replied = msg.reply_to_message
    if not replied:
        return ""
    quoted = (msg.quote.text if getattr(msg, "quote", None) else None) or replied.text or replied.caption or ""
    quoted = quoted.strip()
    if not quoted:
        return ""
    if len(quoted) > 1500:
        quoted = quoted[:1500] + "…"
    return f"[The user is replying to this part: \"{quoted}\"]\n"


def build_turn(items: list[Item], owner: bool) -> Turn | str | None:
    text = "\n".join(it.text for it in items if it.text)
    overrides = {}
    if owner and text:
        try:
            overrides, text = split_overrides(text)
        except ValueError as e:
            return f"Invalid setting in the message: {e}"
    images = [u for it in items for u in it.images]
    files = [f for it in items for f in it.files]
    notes = [n for it in items for n in it.notes]
    if not text and not images and not files and not notes:
        return None
    agent_turn = owner and cfg("agent_mode")
    prefix = quote_context(items[0].msg)
    if notes:
        prefix += "".join(f"[📎 The owner uploaded to the project: {n}]\n" for n in notes)
    mentions = mention_files(text) if agent_turn and text else ""  # for the model only, not saved in memory
    file_blocks = "".join(f"📎 File: {name}\n```\n{body[:MAX_FILE_CHARS]}\n```\n\n" for name, body in files)
    full = prefix + mentions + file_blocks + text
    content: object = full
    if images:
        content = [{"type": "text", "text": full or "What do you see in this image?"}] + \
                  [{"type": "image_url", "image_url": {"url": u}} for u in images]
    if agent_turn:  # the file is saved in the project — memory only needs its name
        saved = "".join(f"📎 File: {n}\n" for n, _ in files)
    else:
        saved = "".join(f"📎 File: {n}\n```\n{b[:20_000]}\n```\n\n" for n, b in files)
    history_text = prefix + "".join("[🖼 image] " for _ in images) + saved + text
    if agent_turn and THINK_HARDER.search(text) and "effort" not in overrides:
        overrides["effort"] = "max"  # "ultrathink" / "think hard", like Claude Code
    return Turn(user_msg_id=items[-1].msg.message_id, content=content, history_text=history_text.strip(),
                overrides=overrides, user_id=items[0].msg.from_user.id if items[0].msg.from_user else None,
                mode="agent" if agent_turn else "chat", project=current_project() if agent_turn else "")


THINK_HARDER = re.compile(r"ultrathink|think\s+(very\s+|really\s+)?(hard|harder|deeply)", re.I)
_MENTION = re.compile(r"(?<![\w@])@([\w./\-]+\.[\w]+|[\w.\-]+/[\w./\-]*)")


def mention_files(text: str) -> str:
    """@path inside a message (coding mode) → attach the file content, or the folder listing, like Claude Code."""
    ws = get_ws()
    out, used = [], 0
    for m in _MENTION.finditer(text):
        path = m.group(1).rstrip(".,\u060c")
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
        out.append(f"[Content of @{path}]\n{body}\n")
    return "".join(out)


pending: dict[int, dict] = {}  # messages waiting for their remaining parts


async def process_items(bot, chat_id: int, items: list[Item]) -> None:
    owner = bool(items[0].msg.from_user) and is_owner_id(items[0].msg.from_user.id)
    turn = build_turn(items, owner)
    if turn is None:
        return
    if isinstance(turn, str):
        await send_html(bot, chat_id, "⚠️ " + esc(turn))
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
        item = "I couldn't download the file — please try again."
    if isinstance(item, str):
        await send_html(context.bot, chat_id, "⚠️ " + esc(item))
        return
    if username := context.bot.username:
        item.text = re.sub(rf"@{re.escape(username)}\b", "", item.text, flags=re.I).strip()
    if is_owner(update):  # expand the owner's snippets (!name) before sending to the model
        item.text = expand_snippets(item.text)

    # a photo album or a long message Telegram split: wait a moment and merge into one
    entry = pending.get(chat_id)
    looks_split = msg.media_group_id is not None or len(msg.text or "") >= 3500

    # Coding mode shortcuts (like Claude Code): "$ cmd" runs a shell command, "# note" saves to project memory
    plain = item.text and not (item.images or item.files or item.notes) and not entry and not looks_split
    if agent_on and plain and item.text.startswith("$ "):
        await run_shell(context.bot, chat_id, item.text[2:].strip())
        return
    if agent_on and plain and item.text.startswith("# ") and "\n" not in item.text.strip():
        add_memory(item.text[2:])
        await send_html(context.bot, chat_id, "Saved to project memory (AGENTS.md).")
        return

    # coding mode is working on a request: a text message from the owner = steering for the lead's next step (like Claude Code)
    running = active.get(chat_id)
    if (running is not None and running.board is not None and is_owner(update) and not running.finishing
            and item.text and not (item.images or item.files or item.notes) and not entry and not looks_split):
        running.inbox.append(item.text)
        await send_html(context.bot, chat_id, "<i>Got it — the lead will see it at the next step, without stopping the work.</i>")
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
    """The user edited their latest message: answer it again (like AI apps do)."""
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


# ================= Buttons =================

async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    ui = active.get(q.message.chat_id) if q.message else None
    if ui and q.message.message_id in ui.message_ids() and ui.stop():
        await q.answer("Stopped")
    else:
        await q.answer()


async def on_regen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = q.message.chat_id
    if chat_id in active:
        await q.answer("Wait for the current reply to finish")
        return
    turn = last_turn.get(chat_id)
    if not turn or q.message.message_id not in turn.response_ids:
        await q.answer("I can only regenerate the latest reply")
        return
    await q.answer("Regenerating")
    await redo(context.bot, chat_id, turn.user_msg_id)


async def on_full_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    text = think_store.get(q.data.split(":", 1)[1])
    if not text:
        await q.answer("The full thinking is no longer stored", show_alert=True)
        return
    await q.answer()
    msg = await context.bot.send_document(q.message.chat_id, InputFile(io.BytesIO(text.encode()), filename="thinking.txt"),
                                          caption="The full chain of thought")
    track(q.message.chat_id, msg.message_id)


# ================= Commands =================

def welcome_html(owner: bool) -> str:
    rows = [
        "<b>Hi — write anything.</b>",
        "",
        "I think out loud, search the web with sources, read images and files (PDF and text),",
        "and you can stop, regenerate or edit a message to get a new answer.",
        f"<b>{NEW_CHAT_BTN}</b> starts fresh.",
    ]
    if owner:
        rows += [
            "",
            "<b>Owner</b>",
            f"Every command is under <b>{MENU_BTN}</b> — no need to type anything.",
            f"<b>{SETTINGS_BTN}</b> holds the model options, <b>{AGENT_BTN}</b> opens the coding agent.",
            "One-message options: <code>effort=max web_search=off your question</code>",
        ]
    return lines_html(*rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    if is_owner(update):
        _set_menu_open(chat_id, False)
    if is_owner(update) and cfg("agent_mode"):
        await send_html(context.bot, chat_id, code_welcome_html(), main_keyboard(True))
        return
    await send_html(context.bot, chat_id, welcome_html(is_owner(update)), main_keyboard(is_owner(update)))


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """New chat: the bot forgets everything before, and the old messages stay in the chat as they are."""
    chat_id = update.effective_chat.id
    ui = active.get(chat_id)
    if ui:
        ui.stop()
    pending.pop(chat_id, None)
    async with chat_locks[chat_id]:
        await drop_old_buttons(context.bot, chat_id)  # the old button no longer belongs to the new chat
        last_turn.pop(chat_id, None)
        if is_owner(update) and cfg("agent_mode"):
            agent_histories.pop(agent_key(chat_id, current_project()), None)
            save_agent_histories()
            await send_html(context.bot, chat_id, lines_html(
                "<b>New coding session</b>",
                f"The files of project <code>{esc(current_project())}</code> and its journal are kept — only the conversation starts over.",
            ), main_keyboard(True))
            return
        histories.pop(chat_id, None)
        save_histories()
        await send_html(context.bot, chat_id, lines_html(
            "<b>New chat</b>",
            "Starting fresh — I don't remember anything from the previous conversation.",
        ), main_keyboard(is_owner(update)))


async def owner_only(update: Update, context) -> bool:
    if is_owner(update):
        return True
    await send_html(context.bot, update.effective_chat.id, "This command is for the bot owner only.")
    return False


async def cmd_resetall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    histories.clear()
    save_histories()
    agent_histories.clear()
    save_agent_histories()
    last_turn.clear()
    await send_html(context.bot, update.effective_chat.id, "<b>The memory of every chat was wiped.</b>")


# ----- Persona -----

capture: dict[int, dict] = {}  # persona input mode across several messages
PERSONA_KB = InlineKeyboardMarkup([[InlineKeyboardButton("Save", callback_data="persona:save"),
                                    InlineKeyboardButton("Cancel", callback_data="persona:cancel")]])


# When a command runs from a Menu button, this holds the text it should see after the command
# ("" for no argument, or what the owner typed when asked) — instead of the button's own label.
menu_input: contextvars.ContextVar = contextvars.ContextVar("menu_input", default=None)


def cmd_text(msg) -> str:
    """The text after the command, for a typed command or a Menu button alike."""
    value = menu_input.get()
    return value if value is not None else after_command(msg.text or msg.caption or "")


def after_command(text: str) -> str:
    """The exact text after the command (only the space after the command is removed)."""
    m = re.match(r"^/\S+", text)
    rest = text[m.end():] if m else text
    return rest[1:] if rest[:1] in (" ", "\n", "\t") else rest


async def read_persona_file(doc, bot) -> str:
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        raise ValueError("The file is too big (limit 2 MB)")
    text = decode_text(await download(bot, doc.file_id))
    if text is None:
        raise ValueError("I couldn't read the file — it must be text (txt) in UTF-8")
    return text


# the edit "target": the persona or the lock rules — both use the same input flow
TARGET_LABEL = {"persona": "persona", "rules": "lock rules", "lead": "lead's identity", "agents": "agents' identity"}
TARGET_VIEW = {"persona": "/persona", "rules": "/rules", "lead": "/leadpersona", "agents": "/agentpersona"}
SET_COMMANDS = ("/setpersona", "/setrules", "/setleadpersona", "/setagentpersona")


def _save_target(target: str, text: str) -> None:
    if target in IDENTITY_FILES:
        save_identity(target, text)
    else:
        (save_persona if target == "persona" else save_lock)(text)


def load_target(target: str) -> str:
    if target in IDENTITY_FILES:
        return load_identity(target)
    return load_persona() if target == "persona" else load_lock()


async def store_control(bot, chat_id: int, text: str, target: str) -> None:
    _save_target(target, text)
    if target == "persona":  # a new persona isn't influenced by old conversation
        histories.clear()
        save_histories()
        last_turn.clear()
    label = TARGET_LABEL[target]
    lines = text.count("\n") + 1
    notes = []
    if target == "lead":
        notes.append("Used by the Coding lead instead of the Chat persona (Lead identity on in Coding settings).")
    elif target == "agents":
        notes.append("Every agent gets it in every task.")
        if len(text) > 2000:
            notes.append(f"⚠️ It's long (~{len(text) // 4:,} tokens) and goes with every agent round — shorter is cheaper.")
    if target in IDENTITY_FILES:
        notes.append(f"To remove it: <code>{TARGET_VIEW[target].replace('/', '/set', 1)} off</code>")
    await send_html(bot, chat_id, lines_html(
        f"✅ <b>{label.capitalize()} saved</b> — exactly as sent",
        f"{len(text):,} characters · {lines:,} lines",
        *notes,
    ))


CAPTURE_REFRESH = 0.8   # seconds: parts that arrive together (Telegram splits long texts) get a single new status message


async def start_capture(bot, chat_id: int, parts: list[str], target: str, note: str = "") -> None:
    prompt = await send_html(bot, chat_id, capture_text(parts, target, note), PERSONA_KB)
    capture[chat_id] = {"parts": parts, "prompt": prompt, "target": target}


def capture_text(parts: list[str], target: str, note: str = "") -> str:
    label = TARGET_LABEL[target]
    chars = sum(len(p) for p in parts)
    lines = [
        f"<b>{label.capitalize()} input mode</b>",
        note or f"Send the {label} now: one message, several messages, or a <code>.txt</code> file.",
        "",
        f"Received: <b>{len(parts)}</b> parts · <b>{chars:,}</b> characters",
    ]
    if parts:  # a short preview of the start and the end, to confirm it's the right text before saving
        text = "\n".join(parts)
        head = " ".join(text[:160].split())
        tail = " ".join(text[-160:].split()) if len(text) > 320 else ""
        preview = esc(head) + (" …" if len(text) > 160 else "") + (f"\n…\n{esc(tail)}" if tail else "")
        lines += [f"<blockquote expandable>{preview}</blockquote>"]
    lines += ["Send more parts, or tap <b>Save</b>."]
    return lines_html(*lines)


async def refresh_capture(bot, chat_id: int) -> None:
    """A fresh status message with Save at the bottom of the chat (the previous one is removed),
    so there's never a need to scroll up to save."""
    await asyncio.sleep(CAPTURE_REFRESH)
    state = capture.get(chat_id)
    if not state:
        return
    old = state.get("prompt")
    state["prompt"] = await send_html(bot, chat_id, capture_text(state["parts"], state.get("target", "persona")),
                                      PERSONA_KB)
    if old:
        with contextlib.suppress(TelegramError):
            await bot.delete_message(chat_id, old.message_id)
        untrack(chat_id, [old.message_id])


def schedule_capture_refresh(bot, chat_id: int) -> None:
    state = capture.get(chat_id)
    if not state:
        return
    if (t := state.get("refresh")) and not t.done():
        t.cancel()
    state["refresh"] = asyncio.create_task(refresh_capture(bot, chat_id))


async def set_control(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str) -> None:
    if not await owner_only(update, context):
        return
    msg, bot, chat_id = update.message, context.bot, update.effective_chat.id
    track(chat_id, msg.message_id)
    raw = (msg.text or msg.caption or "") if menu_input.get() is None else menu_input.get()
    rest = cmd_text(msg)
    replied = msg.reply_to_message
    doc = msg.document or (replied.document if replied else None)
    label = TARGET_LABEL[target]
    if target in IDENTITY_FILES and not doc and rest.strip().lower() in ("off", "clear", "remove", "none"):
        save_identity(target, "")
        fallback = ("the lead goes back to the Chat persona's voice" if target == "lead"
                    else "the agents work without an identity")
        await send_html(bot, chat_id, f"The {label} was removed — {fallback}.")
        return
    try:
        if doc:
            await store_control(bot, chat_id, await read_persona_file(doc, bot), target)
        elif rest.strip():
            if len(raw) >= 3900:  # Telegram split the message — wait for the remaining parts
                await start_capture(bot, chat_id, [rest], target, f"The {label} is long and Telegram split it. Send the remaining parts, if any.")
            else:
                await store_control(bot, chat_id, rest, target)
        elif replied and (replied.text or replied.caption):
            await store_control(bot, chat_id, replied.text or replied.caption, target)
        else:
            await start_capture(bot, chat_id, [], target)
    except ValueError as e:
        await send_html(bot, chat_id, "⚠️ " + esc(str(e)))


async def cmd_setpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "persona")


async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "rules")


async def cmd_setleadpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "lead")


async def cmd_setagentpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_control(update, context, "agents")


async def finish_capture(bot, chat_id: int, save: bool) -> None:
    state = capture.pop(chat_id, None)
    if not state:
        return
    if (t := state.get("refresh")) and not t.done():
        t.cancel()
    target = state.get("target", "persona")
    label = TARGET_LABEL[target]
    if state.get("prompt"):
        with contextlib.suppress(TelegramError):
            await state["prompt"].edit_reply_markup(None)
    if not save:
        await send_html(bot, chat_id, f"Cancelled — the {label} stayed the same.")
    elif not state["parts"]:
        await send_html(bot, chat_id, f"⚠️ I didn't receive any text — the {label} stayed the same.")
    else:
        await store_control(bot, chat_id, "\n".join(state["parts"]), target)


async def capture_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captures persona messages before anything else (even if they start with /)."""
    msg = update.message
    if not msg or msg.chat_id not in capture or not is_owner(update):
        return
    text = msg.text
    track(msg.chat_id, msg.message_id)
    if text in ("/done", "/save"):
        await finish_capture(context.bot, msg.chat_id, True)
        raise ApplicationHandlerStop
    if text == "/cancel":
        await finish_capture(context.bot, msg.chat_id, False)
        raise ApplicationHandlerStop
    if text in (NEW_CHAT_BTN, SETTINGS_BTN, AGENT_BTN, CHAT_BTN, PROJECT_BTN, CODE_NEW_BTN, CODE_SETTINGS_BTN) or \
            (text or "").startswith(SET_COMMANDS):
        if (t := capture.pop(msg.chat_id, {}).get("refresh")) and not t.done():
            t.cancel()
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
            await send_html(context.bot, msg.chat_id, "⚠️ Send text or a .txt file")
            raise ApplicationHandlerStop
    except ValueError as e:
        await send_html(context.bot, msg.chat_id, "⚠️ " + esc(str(e)))
        raise ApplicationHandlerStop from None
    state["parts"].append(part)
    schedule_capture_refresh(context.bot, msg.chat_id)
    raise ApplicationHandlerStop


async def on_persona_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
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
    if target in IDENTITY_FILES and not p.strip():
        fallback = ("The lead uses the Chat persona's voice." if target == "lead" else "The agents work without an identity.")
        await send_html(bot, chat_id, lines_html(f"<b>No {label} set.</b> {fallback}",
                                                 f"Set one: <code>{TARGET_VIEW[target].replace('/', '/set', 1)}</code>"))
        return
    # a file only — an exact copy, with the size in the caption (no text messages in the chat)
    fname = {"persona": "persona.txt", "rules": "rules.txt", "lead": "lead_identity.txt",
             "agents": "agents_identity.txt"}[target]
    m = await bot.send_document(
        chat_id, InputFile(io.BytesIO(p.encode("utf-8")), filename=fname),
        caption=f"Current {label} — {len(p):,} characters · {p.count(chr(10)) + 1:,} lines")
    track(chat_id, m.message_id)


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "persona")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "rules")


async def cmd_leadpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "lead")


async def cmd_agentpersona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await view_control(update, context, "agents")


async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The X-Title sent to OpenRouter. Stored encrypted — it never appears in the repository."""
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    raw = cmd_text(update.message).strip()
    if not raw:
        await send_html(context.bot, chat_id, lines_html(
            "Usage: <code>/settitle App Name</code> (Latin characters)",
            "To turn it off: <code>/settitle off</code>",
        ))
        return
    if raw.lower() in ("off", "none", "remove"):
        save_title("")
        await send_html(context.bot, chat_id, "X-Title removed — it's no longer sent.")
        return
    title = " ".join(raw.split())  # one line, no line breaks
    if not title.isascii():
        await send_html(context.bot, chat_id, lines_html(
            "⚠️ X-Title must use <b>Latin characters</b> (letters, digits and symbols).",
            "HTTP headers don't accept non-ASCII characters, so requests would fail.",
        ))
        return
    save_title(title[:100])
    note = "Stored encrypted" if vault.enabled() else "⚠️ Not encrypted — add PERSONA_KEY to store it encrypted"
    await send_html(context.bot, chat_id, lines_html(
        f"✅ X-Title saved: <code>{esc(title[:100])}</code>",
        note,
    ))


async def cmd_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    t = load_title()
    await send_html(context.bot, chat_id, (f"Current X-Title: <code>{esc(t)}</code>" if t
                                                  else "No X-Title (nothing is sent). Set one with /settitle"))


# ----- Snippets (saved text/files) -----

SNIP_NAME = re.compile(r"\w[\w-]{0,39}")  # \w covers letters of every script
MAX_SNIPPET = 100_000


def expand_snippets(text: str) -> str:
    """Replaces !name with the saved text/file. Only known names are replaced; the rest stays as it is."""
    snips = load_snippets()
    if not snips or "!" not in text:
        return text

    def repl(m):
        s = snips.get(m.group(1))
        if not s:
            return m.group(0)
        if s.get("type") == "file":
            return f"\n[Saved file: {s.get('filename') or m.group(1)}]\n{s.get('content', '')}\n"
        return s.get("content", "")

    return re.sub(r"!(" + SNIP_NAME.pattern + r")", repl, text)


async def cmd_snip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a snippet: /snip name text... — or reply to a message/file with /snip name."""
    if not await owner_only(update, context):
        return
    msg, bot, chat_id = update.message, context.bot, update.effective_chat.id
    track(chat_id, msg.message_id)
    rest = cmd_text(msg)
    parts = rest.split(maxsplit=1)
    if not parts or not SNIP_NAME.fullmatch(parts[0]):
        await send_html(bot, chat_id, lines_html(
            "Usage: <code>/snip name text</code>",
            "Or reply to a message or a file with <code>/snip name</code>.",
            "Then write <code>!name</code> in any message and I'll know what you mean.",
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
            await send_html(bot, chat_id, "⚠️ Write the text after the name, or reply to a message/file.")
            return
    except ValueError as e:
        await send_html(bot, chat_id, "⚠️ " + esc(str(e)))
        return
    snips = load_snippets()
    snips[name] = {"type": typ, "content": content[:MAX_SNIPPET], "filename": fname}
    save_snippets(snips)
    kind = "file" if typ == "file" else "text"
    await send_html(bot, chat_id, lines_html(
        f"✅ Snippet <code>!{esc(name)}</code> saved ({kind})",
        f"{len(content):,} characters",
        f"Use it by writing <code>!{esc(name)}</code> in any message.",
    ))


async def cmd_snippets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update, context):
        return
    chat_id = update.effective_chat.id
    track(chat_id, update.message.message_id)
    snips = load_snippets()
    if not snips:
        await send_html(context.bot, chat_id, lines_html(
            "No snippets yet.",
            "Add one: <code>/snip name text</code>, then write <code>!name</code> in your message.",
        ))
        return
    rows = ["<b>Snippets</b>", ""]
    for name, s in snips.items():
        kind = "" if s.get("type") == "file" else ""
        preview = " ".join((s.get("content") or "").split())[:40]
        rows.append(f"{kind} <code>!{esc(name)}</code> — {esc(preview)}")
    rows += ["", "To delete: <code>/delsnip name</code>"]
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
        await send_html(context.bot, chat_id, f"Snippet <code>!{esc(name)}</code> deleted")
    else:
        await send_html(context.bot, chat_id, "⚠️ No snippet with that name. See /snippets")


# ----- Settings -----

def mode_params(mode: str) -> list[str]:
    """A version's settings: Chat or Coding (shared global ones aren't listed as settings)."""
    want = "code" if mode == "agent" else "chat"
    return [k for k in PARAMS if want in config.PARAM_MODES.get(k, ("chat", "code"))]


def param_label(key: str, mode: str) -> str:
    return config.CODE_LABELS.get(key, PARAMS[key][1]) if mode == "agent" else PARAMS[key][1]


def settings_html(mode: str = "chat") -> str:
    model = cfg("model", mode)
    rows = ["<b>Coding settings</b>" if mode == "agent" else "<b>Settings</b>", "",
            f"Backend: {esc(backend_label())}",
            f"Model: <code>{esc(str(model))}</code> · {esc(provider_text(model))}"]
    if mode == "agent":
        wm = cfg("worker_model", mode)
        rows += [f"Agents: <code>{esc(str(wm))}</code> · {esc(provider_text(wm))}",
                 f"Project: <code>{esc(current_project())}</code>"]
    extra = [f"{k} {cfg(k, mode)}" for k in ("temperature", "top_p", "max_tokens") if cfg(k, mode) is not None]
    if extra:
        rows.append("Advanced: " + " · ".join(esc(x) for x in extra))
    rows.append(f"GitHub: {'connected' if github_tools.enabled() else 'not connected'}")
    rows += ["", "<i>Tap a button to change it.</i>"]
    return lines_html(*rows)


def settings_keyboard(mode: str = "chat") -> InlineKeyboardMarkup:
    tag = "cset" if mode == "agent" else "set"   # the button knows which version it edits, even if the mode changes later

    def btn(label, key, value, current):
        return InlineKeyboardButton(("● " if current == value else "") + label, callback_data=f"{tag}:{key}:{value}")

    def toggle(label, key):
        on = cfg(key, mode)
        return InlineKeyboardButton(f"{label}: {'on' if on else 'off'}", callback_data=f"{tag}:{key}:{'off' if on else 'on'}")

    def head(text):
        return [InlineKeyboardButton(f"— {text} —", callback_data="noop")]

    e, be = cfg("effort", mode), backend_name()
    backend_row = [btn("Paid · OpenRouter", "backend", "openrouter", be), btn("Free · NVIDIA", "backend", "nvidia", be)]
    efforts = [btn({"default": "auto"}.get(x, x), "effort", x, e) for x in EFFORTS]
    if mode != "agent":
        return InlineKeyboardMarkup([
            head("Backend"), backend_row,
            head("Reasoning"), efforts,
            [toggle("Thinking", "show_thinking"), toggle("Web search", "web_search")],
            [toggle("Long replies as a page", "long_pages")],
            [toggle("Text file after each reply", "export_txt")],
            [InlineKeyboardButton("Switch to Coding mode", callback_data="set:agent_mode:on")],
        ])
    perm = cfg("permissions", mode)
    return InlineKeyboardMarkup([
        head("Backend"), backend_row,
        head("Lead reasoning"), efforts,
        head("Agents at once"), [btn(str(n), "max_agents", n, cfg("max_agents", mode)) for n in (2, 3, 4, 6, 8)],
        head("Commands and GitHub changes"),
        [btn("Run automatically", "permissions", "auto", perm), btn("Ask me first", "permissions", "ask", perm)],
        [toggle("Plan first", "plan_mode"), toggle("Thinking", "show_thinking")],
        [toggle("Web search", "web_search"), toggle("Long replies as a page", "long_pages")],
        [toggle("Lead identity", "persona_voice"), toggle("Text file", "export_txt")],
        [InlineKeyboardButton("Back to Chat mode", callback_data="set:agent_mode:off")],
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
        await q.answer("Owner only", show_alert=True)
        return
    tag, key, raw = q.data.split(":", 2)
    mode = "agent" if tag == "cset" else "chat"
    if key not in PARAMS:
        await q.answer()
        return
    save_setting(key, parse_value(key, raw), mode)
    await q.answer("Done ")
    if key == "agent_mode":  # switching versions: the keyboard, the menu and the panel all change
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
            "Usage: <code>/set setting value</code>",
            "Settings: " + " · ".join(f"<code>{k}</code>" for k in keys),
        ))
        return
    try:
        save_setting(args[0], parse_value(args[0], " ".join(args[1:])), mode)
    except ValueError as e:
        await send_html(context.bot, chat_id, "⚠️ Invalid value: " + esc(str(e)))
        return
    await send_html(context.bot, chat_id, settings_html(mode), settings_keyboard(mode))


def shortcut(key: str, value_fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = [key, value_fn(context)]
        await cmd_set(update, context)
    return handler


# ----- Coding mode: commands -----

def _max_agents() -> int:
    try:
        return max(1, min(int(cfg("max_agents", "agent") or AGENT_CFG["max_parallel"]), 8))
    except (TypeError, ValueError):
        return int(AGENT_CFG["max_parallel"])


async def _owner_cmd(update: Update, context) -> int | None:
    """Owner check + tracking the command message. Returns chat_id or None."""
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
        "<b>Coding mode</b>",
        "",
        f"Lead: <code>{esc(str(cfg('model', 'agent')))}</code>",
        f"Agents: <code>{esc(str(cfg('worker_model', 'agent')))}</code> · up to {_max_agents()} at once",
        f"Backend: {esc(backend_label())}",
        f"Project: <code>{esc(ws.name)}</code> · {n} files · {human(size)}",
        "",
        "Describe what you want built or fixed. The lead plans, splits the work across agents, runs and tests it,",
        "then reports back. Write while it works to steer it.",
        "",
        f"<b>{MENU_BTN}</b> has every command (files, undo, diff, GitHub, memory…).",
        "Shortcuts: <code>$ command</code> runs it · <code># note</code> saves to memory · <code>@file</code> attaches a file.",
    )


async def refresh_owner_menu(bot) -> None:
    """The owner's "/" menu changes with the version."""
    if OWNER_ID:
        with contextlib.suppress(TelegramError):
            await bot.set_my_commands(_menu_commands("code" if cfg("agent_mode") else "owner"),
                                      scope=BotCommandScopeChat(OWNER_ID))


async def switch_mode(bot, chat_id: int, on: bool, settings_msg: Message | None = None) -> None:
    _set_menu_open(chat_id, False)  # the main buttons come back
    save_setting("agent_mode", on)
    await refresh_owner_menu(bot)
    if settings_msg is not None:  # an open settings panel turns into the other version's panel
        mode = owner_mode()
        with contextlib.suppress(TelegramError):
            await settings_msg.edit_text(settings_html(mode), parse_mode=ParseMode.HTML,
                                         reply_markup=settings_keyboard(mode))
    if on:
        await send_html(bot, chat_id, code_welcome_html(), main_keyboard(True))
    else:
        await send_html(bot, chat_id, lines_html(
            "<b>Chat mode</b>",
            "Back to normal chat with its own settings. Coding projects and their memory are kept — return with /code.",
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
        "<b>Plan mode on</b>" if on else "<b>Plan mode off</b>",
        "The lead explores and presents a plan, and executes only after you approve it." if on else "The lead executes directly.",
    ))


def list_projects() -> list[str]:
    base = ws_base()
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def projects_html() -> str:
    cur = current_project()
    rows = [f"<b>Current project:</b> <code>{esc(cur)}</code>", ""]
    for name in list_projects()[:30]:
        n, size = Workspace(ws_base(), name).stats()
        cps = checkpoints().count(name)
        rows.append(f"{'●' if name == cur else '○'} <code>{esc(name)}</code> — {n} files · {human(size)}"
                    + (f" · {cps} undo point{'s' if cps != 1 else ''}" if cps else ""))
    rows += ["", "Switch/create: <code>/project name</code> · delete: <code>/project delete name</code>",
             "Clone from GitHub: <code>/clone owner/repo</code>"]
    return lines_html(*rows)


def projects_kb() -> InlineKeyboardMarkup | None:
    cur = current_project()
    rows = [[_btn(("● " if n == cur else "") + n, f"proj:{n}")] for n in list_projects()[:20]
            if len(f"proj:{n}".encode()) <= 64]
    return InlineKeyboardMarkup(rows) if rows else None


async def switch_project(bot, chat_id: int, name: str) -> None:
    ws = get_ws(name)
    save_setting("project", ws.name, "agent")
    n, size = ws.stats()
    await send_html(bot, chat_id, lines_html(
        f"<b>Project:</b> <code>{esc(ws.name)}</code>",
        f"{n} files · {human(size)}" if n else "Empty project — ready to start.",
        "" if cfg("agent_mode") else "Switch on /code to work on it.",
    ))


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args:
        get_ws()  # make sure the current project exists
        await send_html(context.bot, chat_id, projects_html(), projects_kb())
        return
    try:
        if args[0].lower() in ("delete", "rm") and len(args) > 1:
            name = valid_name(args[1])
            if name not in list_projects():
                raise WorkspaceError("No project with that name")
            await send_html(context.bot, chat_id, lines_html(
                f"Delete project <code>{esc(name)}</code> with all its files?", "This can't be undone."),
                InlineKeyboardMarkup([[_btn("Delete permanently", f"projdel:{name}"[:64]), _btn("No", "noop")]]))
            return
        await switch_project(context.bot, chat_id, valid_name(args[0]))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, "⚠️ " + esc(str(e)))


async def on_project_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
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
    await send_html(context.bot, q.message.chat_id, f"Project <code>{esc(name)}</code> deleted.")


async def send_file(bot, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
    try:
        m = await bot.send_document(chat_id, InputFile(io.BytesIO(data), filename=filename), caption=caption or None)
        track(chat_id, m.message_id)
    except TelegramError as e:
        await send_html(bot, chat_id, "⚠️ I couldn't send the file: " + esc(str(e)))


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    path = " ".join(context.args or []) or "."
    try:
        text = ws.tree(400) if path == "." else ws.list_files("**/*", path)
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, "⚠️ " + esc(str(e)))
        return
    n, size = ws.stats()
    head = f"<b>{esc(ws.name)}</b> — {n} files · {human(size)}"
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
            raise WorkspaceError("Give the path of an existing file: <code>/get src/app.py</code>")
        if full.stat().st_size > 45_000_000:
            raise WorkspaceError("The file is bigger than Telegram's limit")
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, "⚠️ " + str(e))
        return
    await send_file(context.bot, chat_id, full.read_bytes(), full.name, f"{ws.rel(full)}")


async def send_zip(bot, chat_id: int) -> None:
    ws = get_ws()
    try:
        data = await asyncio.to_thread(ws.zip_bytes)
    except WorkspaceError as e:
        await send_html(bot, chat_id, "⚠️ " + esc(str(e)))
        return
    n, _ = ws.stats()
    await send_file(bot, chat_id, data, f"{ws.name}.zip", f"{ws.name} — {n} files")


async def cmd_zip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await send_zip(context.bot, chat_id)


async def cmd_sh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a command directly in the project (like ! in Claude Code). Shortcut: start a message with "$ "."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    command = cmd_text(update.message).strip()
    if not command:
        await send_html(context.bot, chat_id, "Usage: <code>/sh ls -la</code> — or just send <code>$ ls -la</code>")
        return
    await run_shell(context.bot, chat_id, command)


async def do_undo(bot, chat_id: int, cp_id: str | None = None) -> None:
    project = current_project()
    top = checkpoints().peek(project)
    if not top:
        await send_html(bot, chat_id, "Nothing to undo in this project.")
        return
    if cp_id and top["id"] != cp_id:
        await send_html(bot, chat_id, lines_html("⚠️ There are newer changes than this one.",
                                                 "Use /undo until you reach it (each undo reverts one request)."))
        return
    restored, skipped = restore_checkpoint(project, top["id"]) or (0, [])
    history = agent_histories[agent_key(chat_id, project)]
    label = top.get("label", "")
    if len(history) >= 2 and history[-1].get("role") == "assistant" \
            and str(history[-2].get("content", ""))[:120] == label:
        history.pop()  # the undone request doesn't stay in the lead's memory
        history.pop()
        save_agent_histories()
    if (t := last_turn.get(chat_id)) and t.checkpoint == top["id"]:
        t.checkpoint = None
    left = checkpoints().count(project)
    await send_html(bot, chat_id, lines_html(
        f"<b>Undid:</b> {esc(top.get('label', '')[:100])}",
        f"Restored {restored} files to how they were before the request" + (f" · couldn't restore {len(skipped)} (too big)" if skipped else ""),
        "<i>Changes made by commands (installs, deleting via shell…) aren't tracked, like in Claude Code.</i>",
        f"{left} undo points left." if left else "",
    ))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await do_undo(context.bot, chat_id)


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    if not diffs:
        await send_html(context.bot, chat_id, "No changes yet.")
        return
    await send_file(context.bot, chat_id, next(reversed(diffs.values())).encode(), "changes.diff", "Diff of the latest request")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The durable project memory (AGENTS.md) — read by the lead and the agents every time, like CLAUDE.md."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    f = ws.root / "AGENTS.md"
    note = cmd_text(update.message).strip()
    if note:
        cur = f.read_text(encoding="utf-8") if f.is_file() else "# AGENTS.md — project memory\n"
        f.write_text(cur.rstrip("\n") + f"\n- {note}\n", encoding="utf-8")
        await send_html(context.bot, chat_id, "Added to project memory (AGENTS.md).")
        return
    if not f.is_file():
        await send_html(context.bot, chat_id, lines_html(
            "No project memory yet.", "Add a note: <code>/memory use pytest for tests</code> (or send <code># note</code>)",
            "or let the lead write it by analyzing the project: /init"))
        return
    text = f.read_text(encoding="utf-8", errors="replace")
    if len(text) > 3500:
        await send_file(context.bot, chat_id, text.encode(), "AGENTS.md", "Project memory")
    else:
        await send_html(context.bot, chat_id, "<b>AGENTS.md</b>\n<pre>" + esc(text) + "</pre>")


async def summarize_messages(msgs: list) -> str:
    def body(m):
        c = m.get("content")
        return c if isinstance(c, str) else "[non-text content]"
    transcript = "\n\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {body(m)}" for m in msgs)
    kw = worker_kwargs()
    res = await llm.stream_chat(client, [
        {"role": "system", "content": "Summarize the following conversation densely and precisely, keeping everything needed to continue it: "
                                      "the goals, decisions, technical details, names and numbers, what was done and what remains. "
                                      "Use the conversation's language, short bullet points, no preamble."},
        {"role": "user", "content": agent.clip(transcript, 150_000)}], kw)
    record_usage({kw["model"]: {**(res.usage or {}), "calls": 1}})
    return res.content.strip()


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compresses the current conversation memory into a summary (saves tokens, keeps the context), like /compact."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    agent_on = bool(cfg("agent_mode"))
    async with chat_locks[chat_id]:
        hist = agent_histories[agent_key(chat_id, current_project())] if agent_on else histories[chat_id]
        if len(hist) < 4:
            await send_html(context.bot, chat_id, "The conversation is short — no need to compact.")
            return
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        before = sum(len(str(m.get("content"))) for m in hist)
        try:
            summary = await summarize_messages(list(hist))
        except Exception as e:
            await send_html(context.bot, chat_id, "⚠️ Couldn't summarize: " + esc(str(e)[:200]))
            return
        if not summary:
            return
        hist.clear()
        hist.append({"role": "user", "content": "[Summary of the earlier conversation — for context]"})
        hist.append({"role": "assistant", "content": summary})
        (save_agent_histories if agent_on else save_histories)()
        last_turn.pop(chat_id, None)
    await send_html(context.bot, chat_id, lines_html(
        "<b>Conversation compacted</b>", f"{before:,} → {len(summary):,} characters — the context is kept as a summary."))


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    data = _read_json(dpath("usage.json"), {})
    rows = ["<b>Usage</b>", "", "<b>This run</b>"]
    rows += [f"<code>{esc(m)}</code> — {agent.usage_line(u)}" for m, u in session_usage.models.items()] or ["—"]
    rows += ["", f"<b>All time</b> (since {esc(data.get('since', '—'))})"]
    total = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
    for m, u in (data.get("models") or {}).items():
        rows.append(f"<code>{esc(m)}</code> — {agent.usage_line(u)}")
        for k in total:
            total[k] += u.get(k, 0)
    rows += ["", f"Total: {agent.usage_line(total)}", "<i>Costs as reported by OpenRouter (NVIDIA is free).</i>"]
    await send_html(context.bot, chat_id, lines_html(*rows))


async def cmd_clone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args or not github_tools.enabled():
        await send_html(context.bot, chat_id, ("Usage: <code>/clone owner/repo [branch]</code> — becomes a new project"
                                                     if github_tools.enabled() else "⚠️ GitHub is not connected (GH_PAT)."))
        return
    try:
        repo = args[0].removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if "/" not in repo:
            repo = f"{await github_tools.login()}/{repo}"
        ws = get_ws(valid_name(repo.split("/")[-1]))
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        msg = await ws.clone(repo, args[1] if len(args) > 1 else None, github_tools.token())
        save_setting("project", ws.name, "agent")
        await send_html(context.bot, chat_id, lines_html(f"{esc(msg)}", f"Current project: <code>{esc(ws.name)}</code>"))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, "⚠️ " + esc(str(e)))


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    args = context.args or []
    if not args or not github_tools.enabled():
        await send_html(context.bot, chat_id, ("Usage: <code>/push owner/repo [branch] [message]</code>"
                                                     if github_tools.enabled() else "⚠️ GitHub is not connected (GH_PAT)."))
        return
    ws = get_ws()
    try:
        repo = args[0].removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if "/" not in repo:
            repo = f"{await github_tools.login()}/{repo}"
        branch = args[1] if len(args) > 1 else "main"
        message = " ".join(args[2:]) or "Update from Telegram"
        created = False
        try:
            res = await ws.push(repo, branch, message, github_tools.token())
        except WorkspaceError as e:
            # the first push of a new project: create the repository (private) and push again
            if "doesn't exist" not in str(e) or repo.split("/")[0].lower() != (await github_tools.login()).lower():
                raise
            out = await github_tools.run_github("gh_create_repo", json.dumps(
                {"name": repo.split("/")[-1], "private": True, "auto_init": False}))
            if "fail" in out.lower() or "error" in out.lower():
                raise WorkspaceError(f"Couldn't create {repo}: {out[:300]}") from None
            created = True
            res = await ws.push(repo, branch, message, github_tools.token())
        await send_html(context.bot, chat_id, lines_html(
            *([f"Created the private repository <code>{esc(repo)}</code>"] if created else []),
            f"<b>Pushed</b> to <code>{esc(repo)}</code> · {esc(branch)} · <code>{res['commit']}</code>",
            f'<a href="{esc(res["url"])}">Open on GitHub</a>' if res["url"].startswith("http") else ""))
    except WorkspaceError as e:
        await send_html(context.bot, chat_id, "⚠️ " + esc(str(e)))


# ----- Claude Code parity: permissions, prompt commands, info commands -----

approvals: dict[str, asyncio.Future] = {}
last_boards: dict[int, "agent.Board"] = {}


def make_approver(bot, chat_id: int, ui: "ResponseUI"):
    """Permission mode "ask": commands and GitHub writes wait for the owner's tap (like Claude Code's prompts)."""
    async def approve(what: str, detail: str) -> bool:
        if ui.allow_all:
            return True
        key = secrets.token_hex(4)
        fut = asyncio.get_running_loop().create_future()
        approvals[key] = fut
        kb = InlineKeyboardMarkup([[_btn("Allow", f"perm:{key}:y"), _btn("Allow all (this request)", f"perm:{key}:a")],
                                   [_btn("Deny", f"perm:{key}:n")]])
        html = f"<b>Approval needed</b> — {esc(what)}\n<pre>{esc(detail[:1500])}</pre>"
        msg = await send_html(bot, chat_id, html, kb)
        before, ui.status = ui.status, "Waiting for your approval…"
        choice = "t"
        try:
            choice = await asyncio.wait_for(fut, 900)
        except asyncio.TimeoutError:
            choice = "t"
        finally:
            approvals.pop(key, None)
            ui.status = before
            verdict = {"y": "✅ Allowed", "a": "✅ Allowed (all for this request)", "n": "❌ Denied",
                       "t": "Timed out — denied"}.get(choice, "❌ Denied")
            if msg:
                with contextlib.suppress(TelegramError):
                    await msg.edit_text(f"{html}\n<b>{verdict}</b>", parse_mode=ParseMode.HTML)
        if choice == "a":
            ui.allow_all = True
        return choice in ("y", "a")
    return approve


async def on_permission_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
        return
    _, key, choice = (q.data.split(":") + ["", ""])[:3]
    fut = approvals.get(key)
    if not fut or fut.done():
        await q.answer("This request is no longer pending")
        return
    fut.set_result(choice)
    await q.answer({"y": "Allowed", "a": "Allowed for this request", "n": "Denied"}.get(choice, "OK"))


INIT_PROMPT = (
    "Analyze this project and create (or improve) AGENTS.md at the project root — the persistent project memory "
    "that you and every agent read at the start of each session. Explore efficiently (tree, manifests, entry points, "
    "tests, CI config). Include: what the project is; exact install/build/run/test commands (verify them by running "
    "the cheap ones); architecture and key directories; conventions (style, naming, libraries); gotchas. Keep it "
    "concise (well under 150 lines), factual and specific to this project — no generic advice. If AGENTS.md already "
    "exists, update it instead of duplicating.")
REVIEW_PROMPT = (
    "Review {target}. Use reviewer agents in parallel for large changes (split by area). Report findings ranked by "
    "severity, each with file:line, the problem, why it matters, and a concrete fix. Skip cosmetic nits. "
    "Do NOT modify any files — review only. End with a one-line verdict.")
SECURITY_PROMPT = (
    "Perform a security review of {target}: injection (SQL, command, path traversal), authentication and "
    "authorization, secrets in code or logs, unsafe deserialization, SSRF, XSS/CSRF, dependency risks and insecure "
    "defaults. Report only real, exploitable issues with severity, file:line, an exploit scenario and the fix. "
    "Do NOT modify any files.")
PR_COMMENTS_PROMPT = (
    "Address the review comments on {target}: read the PR and all its comments (gh_pr get / comments), clone the repo "
    "on the PR's head branch if it isn't in this project yet (git_clone with branch), fix every actionable comment "
    "properly, verify, push to the same branch (git_push), watch CI until green, and summarize what you changed for "
    "each comment.")


def _review_target(args: list[str], default: str) -> str:
    raw = " ".join(args).strip()
    if m := re.fullmatch(r"([\w.\-]+/[\w.\-]+)#(\d+)", raw):
        return f"pull request #{m.group(2)} in {m.group(1)} (use gh_pr get / files / comments)"
    return raw or default


async def run_agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, short: str,
                            overrides: dict | None = None) -> None:
    """Runs a canned request as a coding-mode turn (works from either version)."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await generate(context.bot, chat_id, Turn(None, text, short, overrides or {}, update.effective_user.id,
                                              mode="agent", project=current_project()))


async def cmd_init(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_agent_command(update, context, INIT_PROMPT, "/init — write AGENTS.md")


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _review_target(context.args or [], "the latest changes in this project (git diff/status if it's a git "
                                                "repo, otherwise the files changed recently per the project journal)")
    await run_agent_command(update, context, REVIEW_PROMPT.format(target=target), f"/review {target[:80]}")


async def cmd_security_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _review_target(context.args or [], "the pending changes, or the whole project if there are none")
    await run_agent_command(update, context, SECURITY_PROMPT.format(target=target), f"/security_review {target[:80]}")


async def cmd_pr_comments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args or not re.fullmatch(r"[\w.\-]+/[\w.\-]+#\d+", args[0]):
        if (chat_id := await _owner_cmd(update, context)) is not None:
            await send_html(context.bot, chat_id, "Usage: <code>/pr_comments owner/repo#12</code>")
        return
    await run_agent_command(update, context, PR_COMMENTS_PROMPT.format(target=_review_target(args, "")),
                            f"/pr_comments {args[0]}")


def _tok(n_chars: int) -> int:
    return max(0, round(n_chars / 3.5))  # rough estimate; real tokenization varies by model


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """What the lead will see on the next request, and roughly how many tokens each part costs."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    gh_on = github_tools.enabled()
    web_on = bool(cfg("web_search", "agent"))
    env = make_env(ws, Changes(ws), agent.Board(ws.name), web_on)
    tools = agent.lead_tools(bool(cfg("plan_mode", "agent")), env.web_tools,
                             list(github_tools.GITHUB_TOOLS) if gh_on else [], gh_on)
    persona, identity_rules = lead_identity()
    hist = agent_histories[agent_key(chat_id, ws.name)]
    parts = [("Lead instructions", len(config.LEAD_PROMPT)),
             ("Project tree", len(ws.tree(200))),
             ("Project memory (AGENTS.md)", len(agent.project_memory(ws))),
             ("Project journal", len(journal_text(ws.name))),
             ("Agent types", len(agent.agent_types_text(ws))),
             ("Lead identity" if identity_rules else "Persona (voice)", len(persona or "") + len(identity_rules)),
             ("Tool definitions", len(json.dumps(tools, ensure_ascii=False))),
             (f"Conversation ({len(hist)} messages)", sum(len(str(m.get("content"))) for m in hist))]
    total = sum(n for _, n in parts)
    rows = [f"<b>Context for the next request</b> — <code>{esc(ws.name)}</code>", ""]
    rows += [f"• {esc(name)}: ~{_tok(n):,} tokens" for name, n in parts]
    rows += ["", f"<b>Total:</b> ~{_tok(total):,} tokens (+ your message)",
             "<i>Old code versions are never resent — the lead reads current files on demand, and older requests "
             "live in the short journal. /compact shrinks the conversation.</i>"]
    await send_html(context.bot, chat_id, "\n".join(rows))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    n, size = ws.stats()
    git = "not a git repository"
    if (ws.root / ".git").exists():
        code, out = await ws.git(["status", "--short", "--branch"])
        if not code:
            lines = out.splitlines()
            git = f"{esc(lines[0].lstrip('# ')) if lines else '?'} · {len(lines) - 1} changed"
    running = ws.job(None, "list").count("running")
    lead_id = "own identity" if load_identity("lead").strip() else "Chat persona's voice"
    if not cfg("persona_voice", "agent"):
        lead_id += " (off)"
    rows = [
        "<b>Status</b>", "",
        f"Mode: {'Coding' if cfg('agent_mode') else 'Chat'}",
        f"Backend: {esc(backend_label())}",
        "", "<b>Models</b>",
        f"Lead: <code>{esc(str(cfg('model', 'agent')))}</code> · {esc(provider_text(cfg('model', 'agent')))} · "
        f"reasoning {esc(str(cfg('effort', 'agent')))}",
        f"Agents: <code>{esc(str(cfg('worker_model', 'agent')))}</code> · up to {_max_agents()} at once",
        "", "<b>Project</b>",
        f"<code>{esc(ws.name)}</code> · {n} files · {human(size)}",
        f"git: {git}",
        f"Undo points: {checkpoints().count(ws.name)} · background jobs running: {running}",
        "", "<b>Coding options</b>",
        f"Plan first: {'on' if cfg('plan_mode', 'agent') else 'off'} · commands: {esc(str(cfg('permissions', 'agent')))}",
        f"Lead: {lead_id} · agents: {'own identity' if load_identity('agents').strip() else 'no identity'}",
        f"GitHub: {'connected' if github_tools.enabled() else 'not connected'} · CI watches: {len(load_watches())}",
        "", "<b>Usage this run</b>",
        agent.usage_line(session_usage.total()),
    ]
    await send_html(context.bot, chat_id, "\n".join(rows))


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    jobs = workspace_jobs(ws)
    if not jobs:
        await send_html(context.bot, chat_id, "No background jobs in this project.")
        return
    rows = ["<b>Background jobs</b>", ""]
    rows += [f"• <code>{j.id}</code> {esc(j.status())} — <code>{esc(j.command[:80])}</code>" for j in jobs]
    kb = [[_btn(f"Stop job {j.id}", f"jobkill:{j.id}")] for j in jobs if j.running][:10]
    await send_html(context.bot, chat_id, "\n".join(rows), InlineKeyboardMarkup(kb) if kb else None)


def workspace_jobs(ws: Workspace) -> list:
    import workspace as wsmod
    return list(wsmod._jobs.get(str(ws.real), {}).values())


async def on_job_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
        return
    try:
        out = get_ws().job(int(q.data.split(":", 1)[1]), "kill")
    except (WorkspaceError, ValueError) as e:
        out = str(e)
    await q.answer(out[:180])


async def cmd_todos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    board = last_boards.get(chat_id)
    if not board or not board.todos:
        await send_html(context.bot, chat_id, "No plan yet — the lead writes one for multi-step work.")
        return
    icon = {"pending": "○", "in_progress": "▸", "completed": "✓"}
    rows = [f"<b>Latest plan</b> — <code>{esc(board.project)}</code>", ""]
    rows += [f"{icon[t['status']]} {esc(t['content'])}" for t in board.todos]
    await send_html(context.bot, chat_id, "\n".join(rows))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The coding conversation + project journal as a Markdown file (like /export)."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    project = current_project()
    out = [f"# Coding session — {project}\n"]
    for m in agent_histories[agent_key(chat_id, project)]:
        who = "You" if m["role"] == "user" else "Lead"
        out.append(f"## {who}\n\n{m['content'] if isinstance(m['content'], str) else '[non-text content]'}\n")
    if journal := journal_text(project):
        out.append(f"## Project journal\n\n{journal}\n")
    await send_file(context.bot, chat_id, "\n".join(out).encode(), f"{project}-session.md", "Session export")


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    ws = get_ws()
    rows = ["<b>Agent types</b> — the lead picks one per task", ""]
    for name, spec in agent.agent_types(ws).items():
        flags = " · read-only" if spec.get("readonly") else ""
        flags += f" · model {esc(spec['model'])}" if spec.get("model") else ""
        rows.append(f"• <code>{esc(name)}</code>{flags} — {esc(spec['description'])}")
    rows += ["", "<b>Custom agents:</b> add <code>.agents/NAME.md</code> (or <code>.claude/agents/NAME.md</code>) to the "
             "project with the agent's instructions. Optional front matter: <code>description</code>, "
             "<code>readonly: true</code>, <code>model: …</code>.",
             "<b>Hooks:</b> <code>.agents/hooks.json</code> → <code>{\"after_edit\": \"ruff format {file}\"}</code> runs "
             "after every file edit; any output goes back to the agent."]
    await send_html(context.bot, chat_id, "\n".join(rows))


async def ping_rows() -> list[str]:
    """Checks every API key for real (one tiny streamed request each, all at the same time)."""
    rows = []
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            t0 = time.monotonic()
            r = await http.get(f"{OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"})
        ok = r.status_code == 200
        rows.append(f"{'✅' if ok else '❌'} OpenRouter — {'answers' if ok else f'HTTP {r.status_code}'} "
                    f"({time.monotonic() - t0:.1f}s)")
    except Exception as e:
        rows.append(f"❌ OpenRouter — unreachable ({esc(type(e).__name__)})")
    if not len(nvidia_pool):
        rows.append("⚠️ NVIDIA — no keys (add NVIDIA_KEY_1..3)")
        return rows
    model = config.NVIDIA_MODELS.get(config.WORKER_MODEL, config.WORKER_MODEL)
    results = await asyncio.gather(*(nvidia_pool.ping(s, model) for s in nvidia_pool.slots))
    for slot, (ok, what, secs) in zip(nvidia_pool.slots, results):
        icon = "✅" if ok else "⚠️" if ok is None else "❌"
        rows.append(f"{icon} NVIDIA {esc(slot.name)} — {esc(what)}")
    rows.append("NVIDIA load (last minute): " + " · ".join(
        f"{esc(x['name'])} {x['last_minute']}/{nvidia_pool.rpm}" for x in nvidia_pool.status()))
    return rows


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health check of everything the bot depends on, in short sections."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    def mark(ok: bool | None) -> str:
        return "✅" if ok else "⚠️" if ok is None else "❌"

    rows = ["<b>Health check</b>", "", "<b>API keys</b>", *await ping_rows(), "", "<b>Services</b>"]
    if github_tools.enabled():
        try:
            rows.append(f"✅ GitHub — signed in as <code>{esc(await github_tools.login())}</code>")
        except Exception as e:
            rows.append(f"❌ GitHub — token rejected ({esc(str(e)[:80])})")
    else:
        rows.append("⚠️ GitHub — not connected (add GH_PAT)")
    durable_ok = state_store.enabled and not state_store.last_error
    rows.append(f"{mark(durable_ok or (None if not state_store.enabled else False))} Durable copy — "
                f"{esc(state_store.status())}")
    rows.append(f"{mark(vault.enabled() or None)} Encryption — "
                + ("on" if vault.enabled() else "off (add PERSONA_KEY)"))
    rows += ["", "<b>Models</b>", f"Backend: {esc(backend_label())}"]
    for label, mode, key in (("Chat", "chat", "model"), ("Coding lead", "agent", "model"),
                             ("Coding agents", "agent", "worker_model")):
        m = cfg(key, mode)
        rows.append(f"{label}: <code>{esc(str(m))}</code> · {esc(provider_text(m))}")
    tools = [f"{t} {'✓' if shutil.which(t) else '—'}" for t in ("git", "python3", "node", "npm")]
    free = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else Path(".")).free
    rows += ["", "<b>Machine</b>", " · ".join(tools), f"Free disk: {human(free)}"]
    await send_html(context.bot, chat_id, "\n".join(rows))


async def run_shell(bot, chat_id: int, command: str) -> None:
    if agent._DANGER.search(command):
        await send_html(bot, chat_id, "⛔ Destructive command refused.")
        return
    ws = get_ws()
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    r = await ws.run(command, 300)
    status = "⏱ timed out" if r["timed_out"] else ("✅" if r["code"] == 0 else f"❌ exit {r['code']}")
    out = r["output"].strip() or "(no output)"
    head = f"{status} · <code>{esc(command[:200])}</code> · {r['seconds']:.1f}s"
    if len(out) > 3300:
        await send_html(bot, chat_id, head)
        await send_file(bot, chat_id, out.encode(), "output.txt")
    else:
        await send_html(bot, chat_id, f"{head}\n<pre>{esc(out)}</pre>")


def add_memory(note: str) -> None:
    f = get_ws().root / "AGENTS.md"
    cur = f.read_text(encoding="utf-8") if f.is_file() else "# AGENTS.md — project memory\n"
    f.write_text(cur.rstrip("\n") + f"\n- {note.strip()}\n", encoding="utf-8")


# ----- Buttons on coding mode's final reply -----

async def on_agent_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
        return
    chat_id = q.message.chat_id
    kind, _, rest = q.data.partition(":")
    bot = context.bot
    if kind == "undo":
        await q.answer()
        await do_undo(bot, chat_id, rest)
    elif kind == "diff":
        if not (text := diffs.get(rest)):
            await q.answer("The diff is no longer stored", show_alert=True)
            return
        await q.answer()
        await send_file(bot, chat_id, text.encode(), "changes.diff", "Diff of the changes")
    elif kind == "rep":
        if not (text := reports.get(rest)):
            await q.answer("The reports are no longer stored", show_alert=True)
            return
        await q.answer()
        await send_file(bot, chat_id, text.encode(), "agents-reports.md", "Agent reports")
    elif kind == "zip":
        await q.answer("Preparing the file…")
        await send_zip(bot, chat_id)
    elif kind == "ask":
        key, _, idx = rest.partition(":")
        opts = asks.get(key)
        if not opts or not idx.isdigit() or int(idx) >= len(opts):
            await q.answer("This question is no longer available — type your answer", show_alert=True)
            return
        if chat_id in active:
            await q.answer("Wait for the current reply to finish")
            return
        await q.answer()
        asks.pop(key, None)
        answer = opts[int(idx)]
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(REGEN_KB)
        await send_html(bot, chat_id, f"<b>You chose:</b> {esc(answer)}")
        await generate(bot, chat_id, Turn(None, answer, answer, {}, q.from_user.id, mode="agent",
                                          project=current_project()))
    elif kind == "plan":
        action, _, project = rest.partition(":")
        await q.answer()
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(REGEN_KB)
        if action != "go":
            await send_html(bot, chat_id, "OK, nothing was executed. Send your notes on the plan and I'll revise it.")
            return
        if chat_id in active:
            return
        await send_html(bot, chat_id, "✅ <b>Plan approved</b> — execution started.")
        text = "✅ Plan approved. Execute it fully now, and actually verify the result."
        await generate(bot, chat_id, Turn(None, text, text, {"plan_mode": False}, q.from_user.id, mode="agent",
                                          project=project or current_project()))


# ----- Background CI watching on GitHub -----

watch_lock = asyncio.Lock()
watch_alerts: "OrderedDict[str, dict]" = OrderedDict()


def load_watches() -> dict:
    data = _read_json(dpath("watches.json"), {})
    return data if isinstance(data, dict) else {}


def digest_text(d: dict) -> str:
    parts = []
    for j in d.get("failed_jobs") or []:
        steps = ", ".join(j.get("failed_steps") or []) or "?"
        parts.append(f"❌ {j.get('job')} ({j.get('conclusion')}) — failed steps: {steps}\n{j.get('log', '')}")
    return "\n\n".join(parts) or "(no failure details found)"


async def alert_failure(bot, repo: str, w: dict, r: dict) -> None:
    try:
        d = await github_tools.failure_digest(repo, r["id"])
    except Exception as e:
        d = {"failed_jobs": [], "error": str(e)}
    text = digest_text(d)
    key = new_key()
    _remember(watch_alerts, key, {"repo": repo, "run": r, "digest": text, "chat": w["chat"]}, keep=60)
    jobs = ", ".join(str(j.get("job")) for j in d.get("failed_jobs") or []) or "—"
    html = lines_html(f"<b>CI failed</b> — <code>{esc(repo)}</code>",
                      f"{esc(str(r.get('workflow')))} · {esc(str(r.get('branch')))} · <code>{esc(str(r.get('sha')))}</code>",
                      f"❌ {esc(jobs)}")
    if r.get("url"):
        html += f'\n<a href="{esc(r["url"])}">Open the run on GitHub</a>'
    html += f"\n<blockquote expandable>{esc(text[:1500])}</blockquote>"
    kb = InlineKeyboardMarkup([[_btn("Fix it with agents", f"wfix:{key}"), _btn("Re-run", f"wre:{key}")],
                               [_btn("Stop watching", f"woff:{key}")]])
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
    text = (f"CI failed in repository {repo}\n"
            f"- workflow: {r.get('workflow')} · branch: {r.get('branch')} · commit: {r.get('sha')} · run_id: {r.get('id')}\n"
            f"- link: {r.get('url')}\n\nFailure details:\n{a['digest'][:7000]}\n\n"
            f"Task: fix the root cause of the failure. If the repository isn't cloned in this project, clone it (git_clone) on branch "
            f"{r.get('branch')}. Reproduce the error locally with the workflow's own commands, fix it and verify, then push to the same branch "
            "(git_push) and watch CI (gh_actions action=watch) until it's green. Never disable tests or ignore the error.")
    short = f"Fix the CI failure in {repo} ({r.get('workflow')} on {r.get('branch')}, run {r.get('id')})"
    save_setting("project", project, "agent")
    get_ws(project)
    await send_html(bot, a["chat"], lines_html(f"<b>The lead started fixing the CI failure</b> in <code>{esc(repo)}</code>",
                                               f"Project: <code>{esc(project)}</code>"))
    asyncio.create_task(generate(bot, a["chat"], Turn(None, text, short, {}, OWNER_ID, mode="agent", project=project)))
    return True


async def watch_tick(bot) -> None:
    """Checks the watched repositories: any CI run that finished with a failure → an alert (and an auto-fix if enabled)."""
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
                    f"✅ <b>CI is green again</b> — <code>{esc(repo)}</code>",
                    f"{esc(str(r.get('workflow')))} · {esc(str(r.get('branch')))} · <code>{esc(str(r.get('sha')))}</code>"),
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
    """/watch owner/repo [branch] [auto] · /watch off owner/repo · /watch (the list)."""
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    bot = context.bot
    if not github_tools.enabled():
        await send_html(bot, chat_id, "⚠️ GitHub is not connected — add the GH_PAT secret.")
        return
    args = context.args or []
    async with watch_lock:
        data = load_watches()
        if not args:
            rows = ["<b>CI watching</b>", ""]
            rows += [f"• <code>{esc(r)}</code>" + (f" · {esc(w['branch'])}" if w.get("branch") else "")
                     + (" · auto-fix" if w.get("auto") else "") for r, w in data.items()] or ["No repositories are being watched."]
            rows += ["", "Add: <code>/watch owner/repo [branch] [auto]</code>",
                     "<code>auto</code> = the lead fixes failures and pushes on its own · stop: <code>/watch off owner/repo</code>"]
            await send_html(bot, chat_id, lines_html(*rows))
            return
        off = args[0].lower() in ("off", "stop")
        raw = args[1] if off and len(args) > 1 else args[0]
        repo = raw.removeprefix("https://github.com/").removesuffix(".git").strip("/")
        try:
            if "/" not in repo:
                repo = f"{await github_tools.login()}/{repo}"
        except Exception as e:
            await send_html(bot, chat_id, "⚠️ " + esc(str(e)[:200]))
            return
        if off:
            data.pop(repo, None)
            _write_json(dpath("watches.json"), data)
            await send_html(bot, chat_id, f"Stopped watching <code>{esc(repo)}</code>.")
            return
        extra = [a for a in args[1:]]
        auto = any(a.lower() == "auto" for a in extra)
        branch = next((a for a in extra if a.lower() != "auto"), None)
        try:
            runs = await github_tools.recent_runs(repo, branch, 15)
        except Exception as e:
            await send_html(bot, chat_id, "⚠️ Couldn't read the repository's CI: " + esc(str(e)[:300]))
            return
        data[repo] = {"chat": chat_id, "branch": branch, "auto": auto, "primed": True,
                      "seen": sorted(r["id"] for r in runs if r.get("status") == "completed")}
        _write_json(dpath("watches.json"), data)
    last = runs[0] if runs else None
    icon = {"success": "✅", "failure": "❌", None: ""}.get(last.get("conclusion") if last else None, "") if last else ""
    await send_html(bot, chat_id, lines_html(
        f"<b>Watching CI</b> in <code>{esc(repo)}</code>" + (f" · {esc(branch)}" if branch else ""),
        f"Latest run: {icon} {esc(str(last.get('workflow')))} on {esc(str(last.get('branch')))}" if last else "No runs yet.",
        "Auto-fix is on: on any failure the lead fixes it, pushes, and watches until it's green." if auto
        else "Any failure sends you an alert with a \"Fix it with agents\" button.",
    ))


async def on_watch_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_owner(update):
        await q.answer("Owner only", show_alert=True)
        return
    kind, _, key = q.data.partition(":")
    a = watch_alerts.get(key)
    if not a:
        await q.answer("This alert is old — open /watch", show_alert=True)
        return
    if kind == "wfix":
        await q.answer("Fix started")
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(None)
        await start_ci_fix(context.bot, key)
    elif kind == "wre":
        try:
            await github_tools.rerun(a["repo"], a["run"]["id"])
            await q.answer("Re-running the failed jobs")
        except Exception as e:
            await q.answer(f"Couldn't: {str(e)[:150]}", show_alert=True)
    elif kind == "woff":
        async with watch_lock:
            data = load_watches()
            data.pop(a["repo"], None)
            _write_json(dpath("watches.json"), data)
        await q.answer("Stopped watching")
        with contextlib.suppress(TelegramError):
            await q.edit_message_reply_markup(None)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)


# ================= Startup =================

# The action map: every "action" in config.json points to a function here. Names and menus live in config.json.
# ================= The Menu button: every command as a button, no "/" needed =================

# (label, action, prompt) — a prompt means the command needs input: the bot asks, and the next message is the answer
CHAT_MENU = [
    [("Persona", "persona", None), ("Set persona", "setpersona", None)],
    [("Rules", "rules", None), ("Set rules", "setrules", None)],
    [("Snippets", "snippets", None),
     ("Save snippet", "snip", "Send the snippet as <code>name text</code>. Then write <code>!name</code> in any message to use it.")],
    [("Status", "status", None), ("Health check", "doctor", None), ("Usage", "cost", None)],
    [("App title", "settitle", "Send the app name OpenRouter shows (Latin letters), or <code>off</code> to remove it."),
     ("Wipe all memory", "resetall", "Type <code>YES</code> to wipe the conversation memory of every chat.")],
]
CODE_MENU = [
    [("Files", "files", None), ("Get a file", "get", "Send the file path, e.g. <code>src/app.py</code>."),
     ("Download project", "zip", None)],
    [("Diff", "diff", None), ("Undo", "undo", None), ("Plan mode", "plan", None)],
    [("Review", "review", None), ("Security review", "security_review", None), ("Init memory", "init", None)],
    [("Project memory", "memory", None),
     ("Add a note", "memory", "Send the note to save in the project memory (AGENTS.md)."),
     ("Run a command", "sh", "Send the shell command to run in the project.")],
    [("Clone repo", "clone", "Send the repository (<code>name</code> or <code>owner/name</code>), optionally followed by a branch."),
     ("Push", "push", "Send: <code>repository [branch] [commit message]</code>"),
     ("Watch CI", "watch", "Send: <code>repository [branch] [auto]</code> — or <code>list</code> to see what's watched.")],
    [("Context", "context", None), ("Task list", "todos", None), ("Agents", "agents", None), ("Jobs", "jobs", None)],
    [("Lead identity", "leadpersona", None), ("Set lead identity", "setleadpersona", None)],
    [("Agents identity", "agentpersona", None), ("Set agents identity", "setagentpersona", None)],
    [("PR comments", "pr_comments", "Send the pull request as <code>owner/repo#number</code>."),
     ("Compact", "compact", None), ("Export", "export", None)],
    [("Status", "status", None), ("Health check", "doctor", None), ("Usage", "cost", None)],
]
MENU_ITEMS = {label: (action, prompt) for rows in (CHAT_MENU, CODE_MENU) for row in rows
              for label, action, prompt in row}
LEGACY_ACTIONS = {"new_chat": "new", "code_new": "new", "settings": "settings", "code_settings": "settings",
                  "agent_mode": "agent", "chat_mode": "chat", "project": "project"}
UI_FILE = "ui.json"
awaiting: dict[int, tuple[str, str]] = {}   # chat → (action, label) waiting for the owner's input


def _menu_open() -> set:
    return set(_read_json(dpath(UI_FILE), {}).get("menu_open", []))


def _set_menu_open(chat_id: int, on: bool) -> None:
    data = _read_json(dpath(UI_FILE), {})
    ids = set(data.get("menu_open", []))
    (ids.add if on else ids.discard)(chat_id)
    data["menu_open"] = sorted(ids)
    _write_json(dpath(UI_FILE), data)


def menu_keyboard() -> ReplyKeyboardMarkup:
    rows = CODE_MENU if cfg("agent_mode") else CHAT_MENU
    return ReplyKeyboardMarkup([[label for label, _, _ in row] for row in rows] + [[BACK_BTN]],
                               resize_keyboard=True, is_persistent=True, input_field_placeholder="Pick a command…")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (chat_id := await _owner_cmd(update, context)) is None:
        return
    _set_menu_open(chat_id, True)
    await send_html(context.bot, chat_id, "Pick a command. <b>Back</b> returns to the main buttons.", menu_keyboard())


async def run_action(update: Update, context, action: str, text: str = "") -> None:
    """Runs a command as if it was typed, with `text` as what comes after it."""
    fn = _actions().get(action)
    if not fn:
        return
    token = menu_input.set(text)
    context.args = text.split() if text else []
    try:
        await fn(update, context)
    finally:
        menu_input.reset(token)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before everything else: Menu/Back, the menu's buttons, answers to a button's question, old labels."""
    msg = update.message
    if not msg or msg.text is None or not is_owner(update):
        return
    chat_id, text = msg.chat_id, msg.text.strip()
    if text.startswith("/"):                 # a typed command cancels a pending question
        awaiting.pop(chat_id, None)
        return
    if text == MENU_BTN:
        awaiting.pop(chat_id, None)
        await cmd_menu(update, context)
        raise ApplicationHandlerStop
    if text == BACK_BTN:
        awaiting.pop(chat_id, None)
        _set_menu_open(chat_id, False)
        track(chat_id, msg.message_id)
        await send_html(context.bot, chat_id, "Main buttons.", main_keyboard(True))
        raise ApplicationHandlerStop
    if text in config.LEGACY_BUTTONS:        # a keyboard from before the update
        awaiting.pop(chat_id, None)
        await run_action(update, context, LEGACY_ACTIONS.get(config.LEGACY_BUTTONS[text], "start"))
        raise ApplicationHandlerStop
    if text in MENU_ITEMS and chat_id in _menu_open():
        awaiting.pop(chat_id, None)
        action, prompt = MENU_ITEMS[text]
        if prompt:
            awaiting[chat_id] = (action, text)
            track(chat_id, msg.message_id)
            await send_html(context.bot, chat_id, lines_html(
                f"<b>{esc(text)}</b>", prompt, "<i>Send it as your next message, or tap Back to cancel.</i>"))
        else:
            await run_action(update, context, action)
        raise ApplicationHandlerStop
    if chat_id in awaiting and text not in (NEW_CHAT_BTN, SETTINGS_BTN, AGENT_BTN, CHAT_BTN, PROJECT_BTN,
                                            CODE_NEW_BTN, CODE_SETTINGS_BTN):
        action, label = awaiting.pop(chat_id)
        if action == "resetall":
            if text.lower() in ("yes", "y"):
                await run_action(update, context, "resetall")
            else:
                await send_html(context.bot, chat_id, "Cancelled — nothing was wiped.")
        else:
            await run_action(update, context, action, "" if (action == "watch" and text.lower() == "list") else text)
        raise ApplicationHandlerStop
    awaiting.pop(chat_id, None)


def _actions() -> dict:
    return {
        "start": cmd_start,
        "menu": cmd_menu,
        "new": cmd_new,
        "settings": cmd_settings,
        "setpersona": cmd_setpersona,
        "persona": cmd_persona,
        "setrules": cmd_setrules,
        "rules": cmd_rules,
        "setleadpersona": cmd_setleadpersona,
        "leadpersona": cmd_leadpersona,
        "setagentpersona": cmd_setagentpersona,
        "agentpersona": cmd_agentpersona,
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
        "init": cmd_init,
        "review": cmd_review,
        "security_review": cmd_security_review,
        "pr_comments": cmd_pr_comments,
        "context": cmd_context,
        "status": cmd_status,
        "jobs": cmd_jobs,
        "todos": cmd_todos,
        "export": cmd_export,
        "agents": cmd_agents,
        "doctor": cmd_doctor,
        "set": cmd_set,
        "effort": shortcut("effort", lambda c: (c.args or ["default"])[0]),
        "thinking": shortcut("show_thinking", lambda c: "off" if cfg("show_thinking", owner_mode()) else "on"),
        "search": shortcut("web_search", lambda c: "off" if cfg("web_search", owner_mode()) else "on"),
    }


def _menu_commands(scope: str) -> list:
    """The "/" command menu per scope: default = everyone, owner = the owner in Chat, code = the owner in Coding."""
    want = {"default": {"both", "default"}, "owner": {"both", "default", "owner", "owner_all"},
            "code": {"both", "default", "code", "owner_all"}}[scope]
    out = []
    for c in config.COMMANDS:
        label = c.get("menu_label")
        if label and c.get("menu") in want and c.get("names"):
            out.append(BotCommand(c["names"][0], label))
    return out


async def set_menu(app: Application) -> None:
    # the command list shown by Telegram's "/" button — from config.json
    with contextlib.suppress(TelegramError):
        await app.bot.set_my_commands(_menu_commands("default"), scope=BotCommandScopeDefault())
    await refresh_owner_menu(app.bot)


async def post_init(app: Application) -> None:
    await set_menu(app)
    app.bot_data["watch_task"] = asyncio.create_task(watch_loop(app.bot))  # background CI watching


async def post_shutdown(app: Application) -> None:
    state_store.flush()  # a change made just before stopping still reaches the durable copy
    task = app.bot_data.get("watch_task")
    if task:
        task.cancel()
    kill_all_jobs()  # any server the agents started in the background stops with the bot


def build_app() -> Application:
    builder = (Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown)
               .concurrent_updates(True))
    if api := os.getenv("TELEGRAM_API_BASE"):  # optional: a local Bot API server
        builder = builder.base_url(f"{api}/bot").base_file_url(f"{api}/file/bot")
    app = builder.build()  # concurrent_updates so the stop button works while a reply is running
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT, menu_router), group=-2)
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, capture_messages), group=-1)

    # register the commands from config.json
    actions = _actions()
    for c in config.COMMANDS:
        fn = actions.get(c.get("action"))
        if not fn or not c.get("names"):
            log.warning("Unknown command in config.json: %s", c)
            continue
        app.add_handler(CommandHandler(c["names"], fn))
        if c.get("caption"):  # accept the command written in a file/photo caption
            app.add_handler(MessageHandler(filters.CaptionRegex(rf"^/{re.escape(c['names'][0])}"), fn))

    # the bottom keyboard buttons
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(NEW_CHAT_BTN)}$"), cmd_new))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(SETTINGS_BTN)}$"), cmd_settings))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(AGENT_BTN)}$"), cmd_agent))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CHAT_BTN)}$"), cmd_chat))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(PROJECT_BTN)}$"), cmd_project))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CODE_NEW_BTN)}$"), cmd_new))
    if CODE_SETTINGS_BTN != SETTINGS_BTN:
        app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CODE_SETTINGS_BTN)}$"), cmd_settings))

    app.add_handler(CallbackQueryHandler(on_stop, pattern=r"^stop$"))
    app.add_handler(CallbackQueryHandler(on_regen, pattern=r"^regen$"))
    app.add_handler(CallbackQueryHandler(on_full_thinking, pattern=r"^think:"))
    app.add_handler(CallbackQueryHandler(on_persona_button, pattern=r"^persona:"))
    app.add_handler(CallbackQueryHandler(on_settings_button, pattern=r"^(set:|cset:|noop$)"))
    app.add_handler(CallbackQueryHandler(on_agent_button, pattern=r"^(undo|diff|rep|ask|plan):|^zip$"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^(proj|projdel):"))
    app.add_handler(CallbackQueryHandler(on_watch_button, pattern=r"^(wfix|wre|woff):"))
    app.add_handler(CallbackQueryHandler(on_permission_button, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(on_job_button, pattern=r"^jobkill:"))

    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, on_edited_message))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
                                   on_user_message))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    if OWNER_ID == 0:
        log.warning("OWNER_ID is not set — owner commands won't work for anyone.")
    # the Actions cache can come back empty — bring the persona, rules and settings back from the durable copy
    state_store.restore(DATA_DIR)
    load_state()
    log.info("Bot running via OpenRouter with model %s on %s", cfg("model"), provider_text(cfg("model")))
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

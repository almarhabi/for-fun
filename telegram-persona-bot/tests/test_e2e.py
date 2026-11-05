"""اختبار شامل: خادم تليجرام وهمي + Kimi وهمي، والبوت الحقيقي يشتغل عليهم.

التشغيل: python tests/test_e2e.py
"""
import asyncio, json, os, sys, threading, time, random, shutil, pathlib, re, tempfile
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

PORT = 18555
OWNER = 111
os.environ.update(TELEGRAM_BOT_TOKEN="123:TEST", MOONSHOT_API_KEY="x", OWNER_ID=str(OWNER),
                  TELEGRAM_API_BASE=f"http://127.0.0.1:{PORT}")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import bot, render
from openai.types.chat import ChatCompletionChunk

DATA = pathlib.Path(tempfile.mkdtemp())
bot.DATA_DIR = DATA
bot.PERSONA_FILE, bot.HISTORY_FILE = DATA / "persona.json", DATA / "history.json"
bot.SETTINGS_FILE, bot.SCREEN_FILE = DATA / "settings.json", DATA / "screen.json"
bot.EDIT_INTERVAL = 0.15
bot.GROUP_WAIT = 0.4

# ---------------- خادم تليجرام وهمي ----------------
S = {"lock": threading.Lock(), "updates": [], "uid": 1, "mid": 1000, "msgs": {}, "calls": [], "flaky": False}
BOT_USER = {"id": 999, "is_bot": True, "first_name": "T", "username": "test_bot"}
FILES = {}


def new_msg(chat_id, text=None, markup=None, extra=None):
    S["mid"] += 1
    m = {"message_id": S["mid"], "date": int(time.time()), "chat": {"id": chat_id, "type": "private"},
         "from": BOT_USER}
    if text is not None:
        m["text"] = text
    if markup:
        m["reply_markup"] = markup
        if "keyboard" in markup:  # تليجرام الحقيقي ما يرجع الكيبورد السفلي داخل الرسالة
            S.setdefault("reply_kb", {})[S["mid"]] = markup
            m.pop("reply_markup")
    m.update(extra or {})
    S["msgs"][S["mid"]] = m
    return m


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        for k, v in FILES.items():
            if path.endswith(k):
                self.send_response(200)
                self.send_header("Content-Length", str(len(v)))
                self.end_headers()
                self.wfile.write(v)
                return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1]
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        p = {}
        if "multipart" in ctype:
            for part in raw.split(b"--"):
                m = re.search(rb'name="([^"]+)"(?:; filename="([^"]+)")?\r\n(?:.*\r\n)*?\r\n(.*)\r\n$', part, re.S)
                if m:
                    p[m.group(1).decode()] = (m.group(3) if m.group(2) else m.group(3).decode(errors="replace"))
        elif "json" in ctype:
            p = json.loads(raw or b"{}")
        else:
            for k, v in parse_qs(raw.decode(), keep_blank_values=True).items():
                v = v[0]
                if k not in ("text", "caption"):
                    try:
                        v = json.loads(v)
                    except Exception:
                        pass
                p[k] = v
        with S["lock"]:
            S["calls"].append((method, p))
        if method == "getUpdates":
            deadline = time.time() + min(float(p.get("timeout", 0) or 0), 0.5)
            while True:
                with S["lock"]:
                    off = int(p.get("offset", 0) or 0)
                    ups = [u for u in S["updates"] if u["update_id"] >= off]
                if ups or time.time() > deadline:
                    return self._reply({"ok": True, "result": ups})
                time.sleep(0.03)
        with S["lock"]:
            r = self.api(method, p)
        if isinstance(r, tuple):
            return self._reply(r[0], r[1])
        self._reply({"ok": True, "result": r})

    def api(self, method, p):
        if method == "getMe":
            return BOT_USER
        if method in ("deleteWebhook", "setMyCommands", "sendChatAction", "answerCallbackQuery"):
            return True
        if method == "sendMessage":
            if S["flaky"] and random.random() < 0.15:
                return ({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                         "parameters": {"retry_after": 1}}, 429)
            check_html(p)
            return new_msg(int(p["chat_id"]), p["text"], p.get("reply_markup"))
        if method == "editMessageText":
            if S["flaky"] and random.random() < 0.3:
                return ({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                         "parameters": {"retry_after": 1}}, 429)
            check_html(p)
            m = S["msgs"][int(p["message_id"])]
            if m.get("text") == p["text"] and m.get("reply_markup") == p.get("reply_markup"):
                return ({"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}, 400)
            m["text"] = p["text"]
            m["reply_markup"] = p.get("reply_markup")
            if not p.get("reply_markup"):
                m.pop("reply_markup", None)
            return m
        if method == "editMessageReplyMarkup":
            m = S["msgs"].get(int(p["message_id"]))
            if not m:
                return ({"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}, 400)
            m.pop("reply_markup", None)
            return m
        if method == "deleteMessages":
            for i in p["message_ids"]:
                S["msgs"].pop(int(i), None)
            return True
        if method == "deleteMessage":
            S["msgs"].pop(int(p["message_id"]), None)
            return True
        if method == "sendDocument":
            return new_msg(int(p["chat_id"]), None, None, {"document": {"file_id": "d", "file_unique_id": "d",
                                                                       "file_name": "x.txt"},
                                                          "caption": p.get("caption", "")})
        if method == "getFile":
            fid = p["file_id"]
            return {"file_id": fid, "file_unique_id": fid, "file_size": len(FILES[fid]), "file_path": fid}
        raise RuntimeError("unhandled " + method)


PROBLEMS = []


def check_html(p):
    if p.get("parse_mode") == "HTML":
        t = p["text"]
        if not render.valid_html(t):
            PROBLEMS.append(("invalid html", t[:200]))
        if len(t) > 4096:
            PROBLEMS.append(("too long", len(t)))
        if "**" in t:
            PROBLEMS.append(("raw ** visible", t[:200]))
    if not p.get("text", "x").strip():
        PROBLEMS.append(("empty text", p))


def push(update_kind, payload):
    with S["lock"]:
        S["uid"] += 1
        S["updates"].append({"update_id": S["uid"], update_kind: payload})


USER = {"id": OWNER, "is_bot": False, "first_name": "M"}
OTHER = {"id": 222, "is_bot": False, "first_name": "O"}
CHAT = {"id": OWNER, "type": "private"}
umid = [500]


def user_msg(text=None, user=USER, chat=CHAT, **extra):
    umid[0] += 1
    m = {"message_id": umid[0], "date": int(time.time()), "chat": chat, "from": user}
    if text is not None:
        m["text"] = text
        if text.startswith("/"):
            cmd = text.split()[0]
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    m.update(extra)
    push("message", m)
    return m


def press(data, msg_id, user=USER):
    push("callback_query", {"id": str(random.random()), "from": user, "chat_instance": "c", "data": data,
                            "message": S["msgs"][msg_id]})


def bot_msgs(chat=OWNER):
    return [m for m in S["msgs"].values() if m["chat"]["id"] == chat]


def last_bot(chat=OWNER):
    return bot_msgs(chat)[-1]


def strip(t):
    return render.to_plain(t).replace(render.RLM, "").replace(render.LRM, "")


# ---------------- Kimi وهمي ----------------
MODEL_CALLS = []


def chunk(**delta):
    return ChatCompletionChunk.model_validate({"id": "1", "object": "chat.completion.chunk", "created": 0,
                                               "model": "m", "choices": [{"index": 0, "delta": delta,
                                                                          "finish_reason": None}]})


class FakeStream:
    def __init__(self, deltas, delay):
        self.deltas, self.delay, self.closed = deltas, delay, False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self.deltas:
            await asyncio.sleep(self.delay)
            yield chunk(**d)

    async def close(self):
        self.closed = True


SCRIPT = {"rounds": [], "delay": 0.01, "error": None}


async def fake_create(**kw):
    MODEL_CALLS.append(kw)
    if SCRIPT["error"]:
        raise RuntimeError(SCRIPT["error"])
    deltas = SCRIPT["rounds"].pop(0) if SCRIPT["rounds"] else [{"content": "تمام"}]
    return FakeStream(deltas, SCRIPT["delay"])


bot.client.chat.completions.create = fake_create


async def fake_tool(name, args):
    return '[{"title":"نتيجة"}]', [{"title": "Kimi K3.1 Pro", "url": "https://kimi.ai/k3"},
                                  {"title": "Moonshot", "url": "https://moonshot.ai"}]
bot.run_tool = fake_tool


def text_deltas(text, size=7):
    return [{"content": text[i:i + size]} for i in range(0, len(text), size)]


# ---------------- السيناريوهات ----------------
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, ("" if cond else f"→ {info}"))


async def wait_idle(t=0.3, timeout=20):
    """ينتظر لين ما فيه ردود شغالة ولا رسائل جديدة."""
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(t)
        with S["lock"]:
            pending_updates = S["updates"]
        if not bot.active and not bot.pending and not any(l.locked() for l in bot.chat_locks.values()):
            await asyncio.sleep(t)
            if not bot.active and not bot.pending:
                return
    print("⚠️ timeout waiting idle")


async def scenario():
    # 1) /start
    user_msg("/start")
    await wait_idle()
    m = last_bot()
    check("start: welcome + keyboard", "أهلاً" in m["text"] and m["message_id"] in S.get("reply_kb", {}), m)

    # 2) بحث في النص: الكلام قبل البحث ما ينحذف
    SCRIPT["rounds"] = [
        [{"reasoning_content": "المستخدم يسأل عن كيمي، "}, {"reasoning_content": "لازم أبحث."},
         *text_deltas("خلني أدور لك على آخر الأخبار."),
         {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                          "function": {"name": "web_search", "arguments": '{"query":'}}]},
         {"tool_calls": [{"index": 0, "function": {"arguments": '"كيمي"}'}}]}],
        [{"reasoning_content": "لقيت المعلومات."},
         *text_deltas("## النتيجة\n**Kimi K3.1 Pro** موديل “قوي” جداً ^^\n- نقطة **أولى**\n- second point.\n\n1. خطوة\n2. خطوة")],
    ]
    user_msg("وش أخبار كيمي؟")
    await wait_idle()
    msgs = bot_msgs()
    think, reply = msgs[-2], msgs[-1]
    rt = strip(reply["text"])
    check("search: pre-search text kept", "خلني أدور لك" in rt, rt)
    check("search: post-search text added", "النتيجة" in rt and "Kimi K3.1 Pro" in rt, rt)
    check("search: bold rendered", "<b>Kimi K3.1 Pro</b>" in reply["text"], reply["text"])
    check("search: no ** or ^^ or “", not any(x in reply["text"] for x in ("**", "^^", "“")), reply["text"])
    check("search: «» quotes", "«قوي»" in reply["text"], reply["text"])
    check("search: RTL marks", reply["text"].count(render.RLM) >= 4, reply["text"])
    check("search: sources shown", "المصادر" in reply["text"] and "kimi.ai" in reply["text"], reply["text"])
    check("search: regen button", reply.get("reply_markup", {}).get("inline_keyboard", [[{}]])[0][0].get("callback_data") == "regen", reply)
    check("think: final title", "فكّر لمدة" in think["text"] and "🔎 بحث: كيمي" in think["text"], think["text"])
    check("think: stop button removed", "reply_markup" not in think, think)
    check("think: one message only", sum(1 for m in msgs if "💭" in (m.get("text") or "")) == 1)
    hist = list(bot.histories[OWNER])
    check("history: full reply saved", hist and "خلني أدور لك" in hist[-1]["content"] and "النتيجة" in hist[-1]["content"], hist[-1:])
    check("model: assistant tool msg has reasoning", MODEL_CALLS[-1]["messages"][-2].get("reasoning_content") == "المستخدم يسأل عن كيمي، لازم أبحث.")

    # 3) إيقاف الرد
    SCRIPT["delay"] = 0.05
    SCRIPT["rounds"] = [[{"reasoning_content": "أفكر "}] * 3 + text_deltas("كلمة " * 200, 5)]
    user_msg("اكتب شي طويل")
    await asyncio.sleep(1.2)
    live = last_bot()
    check("stop: stop button live", live.get("reply_markup", {}).get("inline_keyboard", [[{}]])[0][0].get("callback_data") == "stop", live)
    press("stop", live["message_id"])
    await wait_idle()
    reply = last_bot()
    check("stop: stopped notice", "تم إيقاف الرد" in reply["text"], reply["text"])
    check("stop: partial text kept", "كلمة" in reply["text"])
    check("stop: partial saved to history", "كلمة" in bot.histories[OWNER][-1]["content"])
    SCRIPT["delay"] = 0.01

    # 4) إعادة التوليد
    before_ids = list(bot.last_turn[OWNER].response_ids)
    SCRIPT["rounds"] = [text_deltas("رد جديد بعد الإعادة")]
    press("regen", before_ids[-1])
    await wait_idle()
    check("regen: old messages deleted", all(i not in S["msgs"] for i in before_ids))
    check("regen: new reply", "رد جديد بعد الإعادة" in strip(last_bot()["text"]))
    check("regen: history replaced (no dup)", [h["content"] for h in bot.histories[OWNER]].count("اكتب شي طويل") == 1)

    # 5) تعديل الرسالة
    last_user = bot.last_turn[OWNER].user_msg_id
    SCRIPT["rounds"] = [text_deltas("رد على الرسالة المعدلة")]
    push("edited_message", {"message_id": last_user, "date": int(time.time()), "edit_date": int(time.time()),
                            "chat": CHAT, "from": USER, "text": "سؤال معدّل"})
    await wait_idle()
    check("edit: model got edited text", MODEL_CALLS[-1]["messages"][-1]["content"] == "سؤال معدّل", MODEL_CALLS[-1]["messages"][-1])
    check("edit: new reply", "رد على الرسالة المعدلة" in strip(last_bot()["text"]))

    # 6) رد طويل جداً ينقسم
    long_md = "\n\n".join(f"## قسم {i}\n**نقطة مهمة** رقم {i}: " + "نص عربي طويل " * 40 for i in range(12))
    long_md += "\n```python\n" + "print('x')\n" * 300 + "```\nالنهاية"
    SCRIPT["rounds"] = [text_deltas(long_md, 120)]
    user_msg("رد طويل")
    await wait_idle()
    pages = bot.last_turn[OWNER].response_ids
    joined = "".join(strip(S["msgs"][i]["text"]) for i in pages if i in S["msgs"])
    check("long: split into pages", len(pages) >= 4, len(pages))
    check("long: nothing lost", "قسم 0" in joined and "قسم 11" in joined and "النهاية" in joined)
    check("long: regen only on last page", all("reply_markup" not in S["msgs"][i] for i in pages[:-1]))

    # 7) صورة
    FILES["ph1"] = b"\xff\xd8fakejpeg"
    SCRIPT["rounds"] = [text_deltas("شفت الصورة")]
    user_msg(None, photo=[{"file_id": "ph1", "file_unique_id": "ph1", "width": 10, "height": 10, "file_size": 10}],
             caption="وش هذا؟")
    await wait_idle()
    content = MODEL_CALLS[-1]["messages"][-1]["content"]
    check("photo: sent as image", isinstance(content, list) and content[1]["type"] == "image_url", content)
    check("photo: history placeholder", "[🖼 صورة]" in bot.histories[OWNER][-2]["content"])

    # 8) ملف نصي
    FILES["doc1"] = "محتوى الملف السري".encode()
    SCRIPT["rounds"] = [text_deltas("قريت الملف")]
    user_msg(None, document={"file_id": "doc1", "file_unique_id": "doc1", "file_name": "a.txt",
                             "mime_type": "text/plain", "file_size": 30}, caption="لخصه")
    await wait_idle()
    check("doc: content passed", "محتوى الملف السري" in MODEL_CALLS[-1]["messages"][-1]["content"])

    # 9) ألبوم صورتين = رسالة وحدة
    FILES["ph2"], FILES["ph3"] = b"a", b"b"
    n_calls = len(MODEL_CALLS)
    SCRIPT["rounds"] = [text_deltas("صورتين")]
    for f in ("ph2", "ph3"):
        user_msg(None, photo=[{"file_id": f, "file_unique_id": f, "width": 1, "height": 1}], media_group_id="g1")
    await wait_idle()
    check("album: one request with 2 images", len(MODEL_CALLS) == n_calls + 1 and
          sum(1 for c in MODEL_CALLS[-1]["messages"][-1]["content"] if c["type"] == "image_url") == 2)

    # 10) خطأ من الموديل
    SCRIPT["error"] = "boom 400 invalid temperature"
    user_msg("سؤال")
    await wait_idle()
    m = last_bot()
    check("error: friendly + details for owner", "صار خطأ" in m["text"] and "boom" in m["text"], m["text"])
    check("error: regen offered", m.get("reply_markup", {}).get("inline_keyboard", [[{}]])[0][0].get("callback_data") == "regen")
    SCRIPT["error"] = None
    SCRIPT["rounds"] = [text_deltas("اشتغل الحين")]
    press("regen", m["message_id"])
    await wait_idle()
    check("error: regen recovers", "اشتغل الحين" in strip(last_bot()["text"]))

    # 11) الهوية حرف بحرف
    exact = "  اسمك **سالم**\n\n  /أمر_داخلي  \n“اقتباس” ^^ "
    user_msg("/setpersona " + exact)
    await wait_idle()
    check("persona: exact chars", bot.load_persona() == exact, repr(bot.load_persona()))
    # وضع الإدخال المتعدد
    user_msg("/setpersona")
    await wait_idle()
    user_msg("/الجزء الأول يبدأ بشرطة")
    user_msg("الجزء الثاني")
    await wait_idle()
    prompt = [m for m in bot_msgs() if "وضع إدخال الهوية" in (m.get("text") or "")][-1]
    check("persona capture: counter updated", "2" in strip(prompt["text"]), prompt["text"])
    press("persona:save", prompt["message_id"])
    await wait_idle()
    check("persona capture: exact join", bot.load_persona() == "/الجزء الأول يبدأ بشرطة\nالجزء الثاني", repr(bot.load_persona()))
    # من ملف
    FILES["pf"] = "﻿هوية من ملف\nسطر 2 **".encode("utf-8")
    user_msg(None, document={"file_id": "pf", "file_unique_id": "pf", "file_name": "p.txt", "mime_type": "text/plain"},
             caption="/setpersona")
    await wait_idle()
    check("persona file: exact", bot.load_persona() == "هوية من ملف\nسطر 2 **", repr(bot.load_persona()))
    user_msg("/persona")
    await wait_idle()
    docs = [c for c in S["calls"] if c[0] == "sendDocument"]
    check("persona: sends file copy", docs and docs[-1][1].get("document") == "هوية من ملف\nسطر 2 **".encode(), docs[-1][1].get("document") if docs else None)
    check("persona: system prompt has exact persona", "<identity>\nهوية من ملف\nسطر 2 **\n</identity>" in bot.system_prompt())

    # 12) غير المالك ما يقدر يغير الهوية
    user_msg("/setpersona اختراق", user=OTHER, chat={"id": 222, "type": "private"})
    await wait_idle()
    check("persona: non-owner blocked", bot.load_persona() == "هوية من ملف\nسطر 2 **" and "لصاحب البوت" in last_bot(222)["text"])

    # 13) إخفاء التفكير
    user_msg("/settings")
    await wait_idle()
    panel = last_bot()
    press("set:show_thinking:off", panel["message_id"])
    await wait_idle()
    check("settings: toggled", bot.cfg("show_thinking") is False and "❌ التفكير" in json.dumps(S["msgs"][panel["message_id"]].get("reply_markup"), ensure_ascii=False))
    before = len(bot_msgs())
    SCRIPT["rounds"] = [[{"reasoning_content": "سر"}] + text_deltas("بدون تفكير")]
    user_msg("هلا")
    await wait_idle()
    new = bot_msgs()[before:]
    check("hidden thinking: no think message", not any("💭" in (m.get("text") or "") for m in new), [m.get("text") for m in new])
    bot.save_setting("show_thinking", True)

    # 14) محادثة جديدة
    tracked = list(bot.screen[OWNER])
    user_msg("🆕 محادثة جديدة")
    await wait_idle()
    remaining = [i for i in tracked if i in S["msgs"]]
    check("new: memory cleared", not bot.histories.get(OWNER))
    check("new: screen cleared", not remaining, remaining[:5])
    check("new: banner", "محادثة جديدة" in last_bot()["text"])

    # 15) ضغط على الشبكة (429) — الرد لازم يكمل كامل
    S["flaky"] = True
    SCRIPT["delay"] = 0.02
    SCRIPT["rounds"] = [[{"reasoning_content": "فكرة "}] * 10 + text_deltas("بداية الرد " + "كلام " * 150 + " نهاية الرد", 10)]
    user_msg("اختبار ضغط")
    await wait_idle(timeout=60)
    S["flaky"] = False
    check("flaky: full reply delivered", "نهاية الرد" in strip(last_bot()["text"]) and "بداية الرد" in strip(last_bot()["text"]), strip(last_bot()["text"])[:100])

    check("no invalid/raw html sent", not PROBLEMS, PROBLEMS[:5])


async def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bot.load_state()
    app = bot.build_app()
    async with app:
        await bot.set_menu(app)
        await app.start()
        await app.updater.start_polling(poll_interval=0.02, timeout=1)
        try:
            await asyncio.wait_for(scenario(), 240)
        finally:
            await app.updater.stop()
            await app.stop()
    srv.shutdown()
    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} passed")
    sys.exit(0 if ok == len(results) else 1)

asyncio.run(main())

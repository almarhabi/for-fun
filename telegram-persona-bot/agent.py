"""وضع الوكلاء: Kimi قائد يخطط ويوزّع، ووكلاء GLM ينفّذون بالتوازي داخل مساحة عمل حقيقية.

- أدوات المشروع (قراءة/كتابة/تعديل/بحث/أوامر) مشتركة بين القائد والوكلاء.
- dispatch_agents: جدولة مهام بالتوازي مع اعتماديات (depends_on) وحد أقصى للتوازي.
- كل وكيل له نطاق ملفات، ويُمنع يكتب في نطاق وكيل ثاني (Claims)، ويُمنع يكتب فوق ملف ما قراه
  أو تغيّر من بعد ما قراه — عشان الشغل المتوازي ما يدوس على بعضه.
- Board: الحالة الحية اللي تنعرض للمالك (الخطة + الوكلاء + الأوامر + الاستهلاك).
"""

import asyncio
import json
import logging
import platform
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from openai import APIConnectionError, APIStatusError, APITimeoutError

import llm
import render
from render import RLM, esc
from workspace import Changes, Claims, Workspace, WorkspaceError, clip, human

log = logging.getLogger("persona-bot.agent")

# ================= تعريف الأدوات =================

def _fn(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


T_READ = _fn("read_file", "اقرأ ملف من المشروع (بأرقام الأسطر للعرض فقط). للملفات الطويلة استخدم offset/limit.",
             {"path": {"type": "string", "description": "مسار نسبي داخل المشروع"},
              "offset": {"type": "integer", "description": "أول سطر (من 1)"},
              "limit": {"type": "integer", "description": "عدد الأسطر (الافتراضي 2000)"}}, ["path"])
T_WRITE = _fn("write_file", "اكتب ملف كامل (ينشئه مع مجلداته لو مو موجود). لملف موجود: اقرأه أول. "
              "للملفات الضخمة اكتب أول جزء ثم كمّل بـ append=true.",
              {"path": {"type": "string"}, "content": {"type": "string", "description": "المحتوى كامل"},
               "append": {"type": "boolean", "description": "يضيف المحتوى لنهاية الملف بدل الكتابة فوقه"}},
              ["path", "content"])
T_EDIT = _fn("edit_file", "عدّل ملف باستبدال نص موجود بالحرف (old_string لازم يكون فريد في الملف إلا مع replace_all).",
             {"path": {"type": "string"}, "old_string": {"type": "string", "description": "النص الحالي بالضبط"},
              "new_string": {"type": "string", "description": "النص الجديد"},
              "replace_all": {"type": "boolean", "description": "استبدل كل التكرارات"}},
             ["path", "old_string", "new_string"])
T_LIST = _fn("list_files", "اعرض ملفات المشروع بنمط glob (مثل **/*.py أو src/**). يتجاهل node_modules و.git وأمثالها.",
             {"pattern": {"type": "string", "description": "الافتراضي **/*"},
              "path": {"type": "string", "description": "مجلد البداية (الافتراضي الجذر)"}})
T_GREP = _fn("grep", "ابحث بـ regex داخل ملفات المشروع. يرجّع ملف:سطر: النص.",
             {"pattern": {"type": "string"}, "path": {"type": "string"},
              "glob": {"type": "string", "description": "فلترة الملفات مثل *.ts"},
              "ignore_case": {"type": "boolean"}}, ["pattern"])
T_RUN = _fn("run", "شغّل أمر bash داخل المشروع (تثبيت، بناء، اختبار، تشغيل). ينتظر لين يخلص أو تنتهي المهلة. "
            "للخوادم والعمليات اللي ما تنتهي استخدم background=true. لا أوامر تفاعلية.",
            {"command": {"type": "string"},
             "timeout": {"type": "integer", "description": "بالثواني (الافتراضي 180)"},
             "cwd": {"type": "string", "description": "مجلد فرعي (الافتراضي الجذر)"},
             "background": {"type": "boolean", "description": "يشتغل بالخلفية ويرجع رقم job"}}, ["command"])
T_JOB = _fn("job", "تابع الأوامر اللي بالخلفية: output (المخرجات والحالة) أو kill (إيقاف) أو list.",
            {"id": {"type": "integer"}, "action": {"type": "string", "enum": ["output", "kill", "list"]}})
T_TODO = _fn("todo_write", "اكتب/حدّث خطة العمل (تنعرض للمالك مباشرة). أرسل القائمة كاملة كل مرة.",
             {"todos": {"type": "array", "items": {"type": "object", "properties": {
                 "content": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                 "required": ["content", "status"]}}}, ["todos"])
T_DISPATCH = _fn(
    "dispatch_agents",
    "وزّع مهام على وكلاء منفّذين يشتغلون بالتوازي (والمترابطة بالترتيب حسب depends_on). "
    "ينتظر لين يخلصون ويرجّع تقرير كل وكيل والملفات اللي غيّرها. الوكيل ما يشوف المحادثة: "
    "اكتب له كل اللي يحتاجه في instructions.",
    {"tasks": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "description": "معرّف قصير بالإنجليزي (مثل api, db, tests)"},
        "title": {"type": "string", "description": "عنوان قصير يظهر للمالك"},
        "instructions": {"type": "string", "description": "المهمة كاملة: الهدف، السياق، الملفات، العقود، القيود، والتحقق"},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "الملفات/المجلدات/الأنماط اللي يملكها ويكتب فيها هذا الوكيل"},
        "depends_on": {"type": "array", "items": {"type": "string"},
                       "description": "معرّفات مهام لازم تخلص قبله (يوصله تقريرها)"},
        "readonly": {"type": "boolean", "description": "قراءة/بحث/مراجعة فقط بدون تعديل ملفات"}},
        "required": ["id", "title", "instructions"]}}},
    ["tasks"])
T_ASK = _fn("ask_user", "اسأل المالك سؤال (مع خيارات أزرار) لما يكون فيه غموض يغيّر النتيجة جذرياً. ينهي ردك الحالي، وجوابه يوصلك كرسالة.",
            {"question": {"type": "string"},
             "options": {"type": "array", "items": {"type": "string"}, "description": "2-5 خيارات قصيرة"}},
            ["question", "options"])
T_PLAN = _fn("present_plan", "قدّم خطة التنفيذ للمالك ليوافق عليها (بالماركداون). ينهي ردك الحالي.",
             {"plan": {"type": "string"}}, ["plan"])
T_CLONE = _fn("git_clone", "استنسخ مستودع GitHub داخل المشروع (للجذر لو المشروع فاضي، وإلا لمجلد فرعي).",
              {"repo": {"type": "string", "description": "owner/name أو name"},
               "branch": {"type": "string"}, "dest": {"type": "string"}}, ["repo"])
T_PUSH = _fn("git_push", "ارفع المشروع كامل على GitHub (commit لكل التغييرات ثم push). استخدمه فقط لو المالك طلب الرفع.",
             {"repo": {"type": "string", "description": "owner/name أو name"},
              "branch": {"type": "string", "description": "الافتراضي main"},
              "message": {"type": "string", "description": "رسالة الـ commit"},
              "create_if_missing": {"type": "boolean", "description": "ينشئ المستودع لو مو موجود"},
              "private": {"type": "boolean", "description": "لو بينشأ: خاص؟ (الافتراضي نعم)"},
              "force": {"type": "boolean", "description": "رفع إجباري — فقط لو المالك طلبه صراحة"}},
             ["repo"])

WS_READ = [T_READ, T_LIST, T_GREP]
WS_ALL = [T_READ, T_WRITE, T_EDIT, T_LIST, T_GREP, T_RUN, T_JOB]
WS_NAMES = {t["function"]["name"] for t in WS_ALL}
TERMINAL = {"ask_user", "present_plan"}
GH_READ = {"gh_list_repos", "gh_read_file", "gh_list_dir"}


def lead_tools(plan: bool, web_tools: list, gh_tools: list, git: bool) -> list:
    if plan:  # وضع التخطيط: استكشاف وقراءة بس
        return WS_READ + [T_TODO, T_ASK, T_PLAN] + web_tools + [t for t in gh_tools if t["function"]["name"] in GH_READ]
    return WS_ALL + [T_TODO, T_DISPATCH, T_ASK] + web_tools + gh_tools + ([T_CLONE, T_PUSH] if git else [])


def worker_tools(readonly: bool, web_tools: list) -> list:
    base = [T_READ, T_LIST, T_GREP, T_RUN, T_JOB] if readonly else list(WS_ALL)
    return base + web_tools


# ================= الحالة الحية =================

@dataclass
class AgentState:
    id: str
    title: str
    readonly: bool = False
    deps: list = field(default_factory=list)
    files_scope: list = field(default_factory=list)
    status: str = "waiting"      # waiting | running | done | failed | skipped | stopped
    activity: str = ""
    rounds: int = 0
    started: float = 0.0
    ended: float = 0.0
    report: str = ""
    files: set = field(default_factory=set)
    error: str = ""

    @property
    def seconds(self) -> float:
        if not self.started:
            return 0.0
        return (self.ended or time.monotonic()) - self.started


class Usage:
    def __init__(self):
        self.models: dict[str, dict] = {}

    def add(self, model: str, u: dict | None) -> None:
        m = self.models.setdefault(model, {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0})
        m["calls"] += 1
        if u:
            m["prompt"] += u.get("prompt", 0)
            m["completion"] += u.get("completion", 0)
            m["cost"] += u.get("cost", 0.0)

    def total(self) -> dict:
        t = {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}
        for m in self.models.values():
            for k in t:
                t[k] += m[k]
        return t


class Board:
    """كل اللي ينعرض للمالك وهو يشتغل: الخطة، الوكلاء، الأوامر، الاستهلاك."""

    def __init__(self, project: str):
        self.project = project
        self.todos: list[dict] = []
        self.agents: dict[str, AgentState] = {}
        self.commands: list[tuple] = []     # (agent, command, code, timed_out)
        self.batches = 0
        self.tool_calls = 0
        self.started = time.monotonic()
        self.usage = Usage()

    def set_todos(self, todos) -> int:
        clean = []
        for t in todos if isinstance(todos, list) else []:
            if isinstance(t, dict) and str(t.get("content", "")).strip():
                status = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
                clean.append({"content": str(t["content"]).strip()[:200], "status": status})
        self.todos = clean[:30]
        return len(self.todos)

    def busy(self) -> bool:
        return bool(self.todos or self.agents or self.tool_calls)


# ================= بيئة التشغيل =================

WebFn = Callable[[str, str], Awaitable[tuple[str, list]]]


@dataclass
class Env:
    client: object
    ws: Workspace
    changes: Changes
    board: Board
    worker_kw: dict
    max_parallel: int = 4
    max_tasks: int = 12
    worker_rounds: int = 40
    cmd_timeout: int = 180
    max_timeout: int = 900
    web: WebFn | None = None
    web_tools: list = field(default_factory=list)
    gh: Callable[..., Awaitable[str]] | None = None   # (name, raw_args, progress=None)
    gh_token: str = ""
    on_status: Callable[[str], None] | None = None   # سطر الحالة الحي (مثل متابعة CI)
    gh_login: Callable[[], Awaitable[str]] | None = None
    author: tuple = ("Kimi Agent", "agent@users.noreply.github.com")
    headers: dict = field(default_factory=dict)
    worker_prompt: str = ""


class ToolCtx:
    """سياق وكيل واحد (أو القائد): وش قرأ وبأي نسخة، ونطاقه."""

    def __init__(self, env: Env, agent_id: str = "lead", readonly: bool = False,
                 claims: Claims | None = None, state: AgentState | None = None):
        self.env, self.agent_id, self.readonly, self.claims, self.state = env, agent_id, readonly, claims, state
        self.read_hashes: dict[str, str | None] = {}
        # توفير: لو نفس الجزء من نفس الملف انقرأ وما تغيّر، ما نرسله مرة ثانية (يتصفّر لما نختصر السياق)
        self.seen_reads: dict[tuple, str | None] = {}


_DANGER = re.compile(r"(\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|/\*|~|~/|\$HOME|\.\.|\.\./?\S*)(\s|;|&|$))"
                     r"|\bmkfs\b|\b(shutdown|reboot|poweroff|halt)\b|:\(\)\s*\{|\bdd\s+[^|;]*of=/dev/"
                     r"|\bchmod\s+-R\s+\S+\s+/(\s|$)|>\s*/dev/sd")


def _fmt_run(command: str, r: dict, timeout: float) -> str:
    status = f"⏱ انتهت المهلة ({timeout:.0f}ث) وانوقف الأمر — لو هو خادم استخدم background=true" if r["timed_out"] \
        else f"exit {r['code']}"
    return f"$ {command}\n{clip(r['output']) or '(بدون مخرجات)'}\n[{status} · {r['seconds']:.1f}ث]"


async def exec_ws(ctx: ToolCtx, name: str, args: dict) -> str:
    """ينفّذ أداة مشروع ويرجّع نص للموديل. الأخطاء المفهومة ترجع نص بدل ما تطيّح الوكيل."""
    env, ws = ctx.env, ctx.env.ws
    try:
        if name == "read_file":
            full = ws.resolve(args.get("path"))
            offset, limit = args.get("offset") or 1, args.get("limit") or 2000
            if full.is_file():
                rel, h = ws.rel(full), ws.file_hash(full)
                key = (rel, int(offset), int(limit))
                if ctx.seen_reads.get(key) == h:
                    ctx.read_hashes[rel] = h
                    return (f"[{rel}: نفس المحتوى اللي قريته قبل بهذي الجلسة وما تغيّر — "
                            "ارجع لنتيجة القراءة السابقة بدل الإعادة]")
            out = ws.read(args.get("path"), offset, limit)
            if full.is_file():
                ctx.read_hashes[rel] = h
                ctx.seen_reads[key] = h
            return out
        if name == "list_files":
            return ws.list_files(args.get("pattern") or "**/*", args.get("path") or ".")
        if name == "grep":
            return ws.grep(str(args.get("pattern", "")), args.get("path") or ".", args.get("glob"),
                           bool(args.get("ignore_case")))
        if name in ("write_file", "edit_file"):
            if ctx.readonly:
                return "⛔ مهمتك قراءة فقط — ما تقدر تعدّل ملفات. اذكر التعديلات المقترحة في تقريرك."
            full = ws.resolve(args.get("path"))
            rel = ws.rel(full)
            if ctx.claims and (err := ctx.claims.check(ctx.agent_id, rel)):
                return "⛔ " + err
            if full.is_file():
                known, cur = ctx.read_hashes.get(rel, "∅"), ws.file_hash(full)
                if name == "write_file" and not args.get("append") and known == "∅":
                    return (f"⛔ {rel} موجود — اقرأه بـ read_file قبل ما تكتب فوقه "
                            "(أو استخدم edit_file للتعديل الدقيق).")
                if known not in ("∅", cur):
                    return f"⛔ {rel} تغيّر من بعد ما قريته (وكيل ثاني أو أمر) — اقرأه من جديد قبل التعديل."
            env.changes.note(full, ctx.agent_id)
            if name == "write_file":
                out = ws.write(args.get("path"), str(args.get("content", "")), bool(args.get("append")))
            else:
                out = ws.edit(args.get("path"), str(args.get("old_string", "")), str(args.get("new_string", "")),
                              bool(args.get("replace_all")))
            ctx.read_hashes[rel] = ws.file_hash(full)
            if ctx.state is not None:
                ctx.state.files.add(rel)
            return out
        if name == "run":
            command = str(args.get("command", "")).strip()
            if not command:
                return "⛔ الأمر فاضي"
            if _DANGER.search(command):
                return "⛔ أمر خطير مرفوض (حذف/تخريب خارج المشروع). اشتغل داخل مجلد المشروع فقط."
            if args.get("background"):
                return await ws.start_job(command, args.get("cwd") or ".")
            try:
                timeout = float(args.get("timeout") or env.cmd_timeout)
            except (TypeError, ValueError):
                timeout = env.cmd_timeout
            timeout = max(5.0, min(timeout, float(env.max_timeout)))
            r = await ws.run(command, timeout, args.get("cwd") or ".")
            env.board.commands.append((ctx.agent_id, command, r["code"], r["timed_out"]))
            return _fmt_run(command, r, timeout)
        if name == "job":
            return ws.job(args.get("id"), args.get("action") or "output")
    except WorkspaceError as e:
        return f"⛔ {e}"
    except (OSError, ValueError, TypeError) as e:
        return f"⛔ فشلت الأداة: {type(e).__name__}: {e}"
    return f"⛔ أداة غير معروفة: {name}"


# ================= الوكلاء المنفّذين =================

def _transient(e: Exception) -> bool:
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(e, APIStatusError) and e.status_code in (408, 409, 425, 429, 500, 502, 503, 504)


async def call_model(env: Env, messages: list, kw: dict, tool_kwargs: dict, on_delta=None,
                     on_retry: Callable[[str], None] | None = None) -> llm.Completion:
    """استدعاء مع إعادة محاولة للأخطاء المؤقتة (ضغط المزود، انقطاع)."""
    for attempt in range(3):
        try:
            res = await llm.stream_chat(env.client, messages, kw, tool_kwargs, env.headers, on_delta)
            env.board.usage.add(kw["model"], res.usage)
            return res
        except Exception as e:
            if attempt == 2 or not _transient(e):
                raise
            if on_retry:
                on_retry(f"⏳ ضغط على المزود — إعادة محاولة {attempt + 1}")
            await asyncio.sleep(4 * (attempt + 1) ** 2)
    raise RuntimeError("unreachable")


def trim_context(messages: list, limit: int = 240_000, target: int = 140_000, keep_last: int = 8) -> bool:
    """توفير التوكنز بدون ما ينقص الفهم: لما تكبر الجلسة فوق limit نختصر نتائج الأدوات القديمة ومحتوى
    الملفات المكتوبة قديماً (موجودة في المشروع أصلاً) لين ننزل تحت target. نختصر على دفعات كبيرة ونادرة
    عشان بداية المحادثة تبقى ثابتة بين الجولات (يستفيد من الكاش عند المزود). يرجّع True لو اختصر."""
    def size(m):
        return len(str(m.get("content") or "")) + sum(len(c["function"]["arguments"]) for c in m.get("tool_calls") or [])

    total = sum(size(m) for m in messages)
    if total <= limit:
        return False
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") in ("tool", "assistant")]
    for i in tool_idx[: -keep_last * 2]:
        m = messages[i]
        before = size(m)
        if m["role"] == "tool" and len(m.get("content") or "") > 400:
            m["content"] = m["content"][:250] + "\n…[نتيجة قديمة انقصّت لتوفير المساحة — أعد الأداة لو تحتاجها]"
        for c in m.get("tool_calls") or []:
            a = llm.parse_args(c["function"]["arguments"])
            if a and len(c["function"]["arguments"]) > 600:
                for k in ("content", "new_string", "old_string", "instructions"):
                    if isinstance(a.get(k), str) and len(a[k]) > 200:
                        a[k] = a[k][:120] + "…[مختصر — المحتوى محفوظ في المشروع]"
                c["function"]["arguments"] = json.dumps(a, ensure_ascii=False)
        total -= before - size(m)
        if total <= target:
            break
    return True


_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]{1,200})')


def _progress(st: AgentState):
    def on_delta(raw: str, reasoning: str, calls: dict) -> None:
        if calls:
            c = calls[max(calls)]
            m = _PATH_RE.search(c["args"])
            size = len(c["args"])
            approx = f"~{size / 1000:.0f}k حرف" if size >= 1000 else ""
            if c["name"] in ("write_file", "edit_file") and m:
                st.activity = f"✍️ يكتب {m.group(1)}" + (f" ({approx})" if approx else "")
            elif c["name"]:
                st.activity = describe(c["name"], c["args"]) or f"🛠 {c['name']}"
        elif raw.strip():
            st.activity = "📝 يكتب التقرير…"
        elif reasoning:
            st.activity = "💭 يفكر…"
    return on_delta


def env_block(env: Env) -> str:
    tools = [t for t in ("python3", "pip", "node", "npm", "git", "go", "cargo", "java", "gcc", "docker") if shutil.which(t)]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<env>\nالمشروع: {env.ws.name} — المجلد الحالي هو جذر المشروع، وكل المسارات نسبية له\n"
            f"التاريخ: {now} · النظام: {platform.system()} · أدوات متوفرة: {', '.join(tools) or '—'}\n"
            f"البحث في الإنترنت: {'متاح' if env.web else 'مقفل'}\n</env>")


def project_memory(ws: Workspace, limit: int = 20_000) -> str:
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = ws.root / name
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace")
            return f'<project_memory file="{name}">\n{clip(text, limit)}\n</project_memory>'
    return ""


def worker_system(env: Env) -> str:
    return "\n\n".join(x for x in (
        env.worker_prompt, env_block(env),
        f"<project_tree>\n{env.ws.tree(150)}\n</project_tree>", project_memory(env.ws)) if x)


def worker_brief(st: AgentState, instructions: str, others: list[AgentState], deps: list[AgentState]) -> str:
    parts = [f"معرّف المهمة: {st.id}", f"العنوان: {st.title}"]
    if st.readonly:
        parts.append("⚠️ مهمة قراءة/مراجعة فقط: لا تعدّل أي ملف.")
    elif st.files_scope:
        parts.append("نطاق ملفاتك (تكتب فيه): " + "، ".join(st.files_scope))
    if others:
        parts.append("وكلاء شغالين بالتوازي معك (لا تلمس ملفاتهم):\n" + "\n".join(
            f"- {o.id}: {o.title}" + (f" — {', '.join(o.files_scope)}" if o.files_scope else "") for o in others))
    parts.append("## المهمة\n" + instructions.strip())
    for d in deps:
        files = ", ".join(sorted(d.files)) or "—"
        parts.append(f"## نتيجة المهمة اللي تعتمد عليها: {d.id} — {d.title}\n"
                     f"الملفات اللي غيّرها: {files}\n{clip(d.report, 3500)}")
    return "\n\n".join(parts)


async def run_worker(env: Env, st: AgentState, brief: str, claims: Claims) -> None:
    ctx = ToolCtx(env, st.id, st.readonly, claims, st)
    tools = worker_tools(st.readonly, env.web_tools if env.web else [])
    messages = [{"role": "system", "content": worker_system(env)}, {"role": "user", "content": brief}]
    on_delta = _progress(st)

    def on_retry(msg: str) -> None:
        st.activity = msg

    for rnd in range(env.worker_rounds + 1):
        if rnd == env.worker_rounds:
            messages.append({"role": "user", "content": "⏱ خلصت جولاتك. اكتب تقريرك النهائي الحين بدون أي أدوات."})
            tk = {"tools": tools, "tool_choice": "none"}
        else:
            tk = {"tools": tools}
        if trim_context(messages, limit=200_000, target=120_000):
            ctx.seen_reads.clear()  # النتائج القديمة انقصّت — القراءة الجاية لازم ترجع كاملة
        st.activity = "💭 يفكر…"
        res = await call_model(env, messages, env.worker_kw, tk, on_delta, on_retry)
        st.rounds += 1
        if not res.calls:
            st.report = res.content.strip() or "(الوكيل خلص بدون ما يكتب تقرير)"
            st.status = "done"
            st.activity = ""
            return
        messages.append(llm.assistant_message(res.content, res.calls, res.details))
        for c in res.calls:
            env.board.tool_calls += 1
            args = llm.parse_args(c["args"])
            st.activity = describe(c["name"], c["args"]) or f"🛠 {c['name']}"
            if args is None:
                result = ("⛔ وسائط الأداة مو JSON صالح (غالباً انقطعت لأنها طويلة). "
                          "لو تكتب ملف ضخم: اكتب جزء بـ write_file ثم كمّل بـ append=true.")
            elif c["name"] in WS_NAMES:
                result = await exec_ws(ctx, c["name"], args)
            elif env.web and c["name"] in {t["function"]["name"] for t in env.web_tools}:
                result, _ = await env.web(c["name"], c["args"])
            else:
                result = f"⛔ أداة غير متاحة لك: {c['name']}"
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})


_SLUG = re.compile(r"[^A-Za-z0-9_\-]+")


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> bool:
        state[n] = 1
        path.append(n)
        for d in graph[n]:
            if state.get(d) == 1 or (state.get(d) is None and visit(d)):
                return True
        state[n] = 2
        path.pop()
        return False

    for n in graph:
        if state.get(n) is None and visit(n):
            return path
    return None


async def dispatch(env: Env, raw_tasks) -> str:
    """يشغّل دفعة مهام: المستقلة بالتوازي (حد max_parallel)، والمعتمدة بعد ما تخلص اعتمادياتها."""
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return "⛔ tasks لازم تكون قائمة مهام"
    if len(raw_tasks) > env.max_tasks:
        return f"⛔ أقصى {env.max_tasks} مهمة بالدفعة الوحدة — اجمع المهام الصغيرة أو قسّمها على دفعات"
    board = env.board
    rename: dict[str, str] = {}
    specs = []
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            return f"⛔ المهمة رقم {i + 1} مو كائن"
        raw_id = str(t.get("id") or f"t{i + 1}")
        tid = _SLUG.sub("-", raw_id).strip("-")[:24] or f"t{i + 1}"
        base, n = tid, 2
        while tid in board.agents or tid in rename.values():
            tid, n = f"{base}-{n}", n + 1
        rename[raw_id] = tid
        instructions = str(t.get("instructions") or "").strip()
        if not instructions:
            return f"⛔ المهمة «{raw_id}» بدون instructions — الوكيل ما يشوف المحادثة، اكتب له المهمة كاملة"
        files = [str(f) for f in (t.get("files") or []) if isinstance(f, str)][:40]
        deps = [str(d) for d in (t.get("depends_on") or []) if isinstance(d, (str, int))]
        specs.append((tid, str(t.get("title") or raw_id)[:80], instructions, files, deps, bool(t.get("readonly"))))
    graph = {}
    for tid, _, _, _, deps, _ in specs:
        unknown = [d for d in deps if d not in rename]
        if unknown:
            return f"⛔ depends_on فيه معرّفات مو موجودة بنفس الدفعة: {', '.join(unknown)}"
        graph[tid] = [rename[d] for d in deps]
    if cyc := _cycle(graph):
        return f"⛔ فيه اعتماد دائري بين المهام: {' → '.join(cyc)}"

    board.batches += 1
    states: dict[str, AgentState] = {}
    instr: dict[str, str] = {}
    claims = Claims()
    for tid, title, instructions, files, _, ro in specs:
        st = AgentState(tid, title, ro, graph[tid], files)
        states[tid] = board.agents[tid] = st
        instr[tid] = instructions
        if not ro:
            claims.register(tid, files)
    done = {tid: asyncio.Event() for tid in states}
    sem = asyncio.Semaphore(max(1, env.max_parallel))

    async def run_one(st: AgentState) -> None:
        try:
            if st.deps:
                st.activity = "⏳ ينتظر " + "، ".join(st.deps)
                for d in st.deps:
                    await done[d].wait()
                bad = [d for d in st.deps if states[d].status != "done"]
                if bad:
                    st.status = "skipped"
                    st.report = f"ما اشتغلت لأن المهام اللي تعتمد عليها ما نجحت: {', '.join(bad)}"
                    st.activity = ""
                    return
            st.activity = "⏳ بالطابور"
            async with sem:
                st.status, st.started, st.activity = "running", time.monotonic(), "💭 يقرأ المهمة…"
                others = [o for o in states.values() if o is not st and o.status in ("waiting", "running")
                          and st.id not in o.deps and o.id not in st.deps]
                brief = worker_brief(st, instr[st.id], others, [states[d] for d in st.deps])
                try:
                    await run_worker(env, st, brief, claims)
                except asyncio.CancelledError:
                    st.status, st.activity = "stopped", ""
                    raise
                except Exception as e:
                    log.warning("worker %s failed", st.id, exc_info=True)
                    st.status, st.activity = "failed", ""
                    st.error = f"{type(e).__name__}: {str(e)[:300]}"
                    st.report = st.report or f"فشل الوكيل: {st.error}"
        finally:
            if st.started and not st.ended:
                st.ended = time.monotonic()
            claims.release(st.id)
            done[st.id].set()

    await asyncio.gather(*(run_one(s) for s in states.values()))

    results = []
    for st in states.values():
        results.append({"id": st.id, "title": st.title, "status": st.status,
                        "files_changed": sorted(st.files), "rounds": st.rounds,
                        "seconds": round(st.seconds), "report": clip(st.report, 5000)})
    ok = sum(r["status"] == "done" for r in results)
    note = (f"{ok}/{len(results)} نجحت. راجع التقارير، اقرأ الملفات المهمة، وشغّل التحقق قبل ما تعتبرها خلصت."
            if ok else "ولا مهمة نجحت — شوف الأسباب وقرر: تصلحها بنفسك أو تعيد التوزيع بتعليمات أوضح.")
    return json.dumps({"results": results, "note": note}, ensure_ascii=False)


# ================= أدوات القائد =================

async def resolve_repo(env: Env, repo: str) -> str:
    repo = (repo or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if "/" in repo or not env.gh_login:
        return repo
    return f"{await env.gh_login()}/{repo}"


async def git_tool(env: Env, name: str, args: dict) -> str:
    if not env.gh_token:
        return "⛔ GitHub غير مربوط — المالك لازم يضيف السر GH_PAT."
    try:
        repo = await resolve_repo(env, str(args.get("repo", "")))
        if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
            return "⛔ اسم المستودع لازم يكون owner/name"
        if name == "git_clone":
            dest = args.get("dest") or ("." if env.ws.is_empty() else repo.split("/")[-1])
            return await env.ws.clone(repo, args.get("branch"), env.gh_token, dest)
        branch = str(args.get("branch") or "main")
        message = str(args.get("message") or "Update from Telegram agent")
        try:
            res = await env.ws.push(repo, branch, message, env.gh_token, env.author, bool(args.get("force")))
        except WorkspaceError as e:
            if not (args.get("create_if_missing") and env.gh and "مو موجود" in str(e)):
                raise
            created = await env.gh("gh_create_repo", json.dumps({
                "name": repo.split("/")[-1], "private": args.get("private", True), "auto_init": False}))
            if "فشل" in created:
                return f"⛔ ما قدرت أنشئ المستودع: {created}"
            res = await env.ws.push(repo, branch, message, env.gh_token, env.author, False)
            res["created_repo"] = True
        return json.dumps(res, ensure_ascii=False)
    except WorkspaceError as e:
        return f"⛔ {e}"


async def execute_lead(env: Env, ctx: ToolCtx, name: str, raw_args: str) -> tuple[str, list]:
    """أدوات القائد (ما عدا ask_user و present_plan اللي ينهون الرد ويتعامل معها البوت)."""
    args = llm.parse_args(raw_args)
    if args is None:
        return ("⛔ وسائط الأداة مو JSON صالح (غالباً انقطعت لأنها طويلة). لو تكتب ملف ضخم: "
                "اكتب جزء بـ write_file ثم كمّل بـ append=true. لو مهام طويلة: وزّعها على دفعتين."), []
    if name == "todo_write":
        n = env.board.set_todos(args.get("todos"))
        return f"تم تحديث الخطة ({n} مهام).", []
    if name == "dispatch_agents":
        return await dispatch(env, args.get("tasks")), []
    if name in WS_NAMES:
        return await exec_ws(ctx, name, args), []
    if name in ("git_clone", "git_push"):
        return await git_tool(env, name, args), []
    if env.web and name in {t["function"]["name"] for t in env.web_tools}:
        return await env.web(name, raw_args)
    if env.gh and name.startswith("gh_"):
        return await env.gh(name, raw_args, progress=env.on_status), []
    return f"⛔ أداة غير معروفة: {name}", []


def lead_system(env: Env, base_prompt: str, plan: bool, persona: str | None, github_note: str,
                journal: str = "") -> str:
    parts = [base_prompt]
    if plan:
        parts.append(
            "## ⚠️ وضع التخطيط مفعّل\n"
            "المالك يبي يشوف الخطة ويوافق قبل أي تنفيذ. الحين: استكشف وافهم (قراءة فقط — ما تقدر تكتب "
            "ولا تشغّل أوامر ولا توزّع وكلاء)، ثم قدّم خطة تنفيذ مفصّلة بـ `present_plan`: فهمك للمطلوب وافتراضاتك، "
            "البنية والتصميم، تقسيم المهام على الوكلاء (مين يسوي وش، وبأي ترتيب، وأي ملفات)، وطريقة التحقق. "
            "لو الطلب مجرد سؤال ما يحتاج تنفيذ، جاوب مباشرة.")
    parts.append(env_block(env).replace(
        "</env>", f"الوكلاء المنفّذين: {env.worker_kw.get('model')} — حتى {env.max_parallel} بالتوازي، "
                  f"وحتى {env.max_tasks} مهمة بالدفعة\nGitHub: {'متصل' if env.gh_token else 'غير مربوط'}\n</env>"))
    parts.append(f"<project_tree>\n{env.ws.tree(200)}\n</project_tree>")
    if mem := project_memory(env.ws):
        parts.append(mem)
    if journal:
        parts.append("<project_journal>\nسجل مختصر لكل الطلبات السابقة في هذا المشروع (الأقدم أولاً). "
                     "الكود الحالي في الملفات هو المرجع؛ هذا السجل للسياق فقط.\n"
                     f"{journal}\n</project_journal>")
    if github_note and env.gh_token:
        parts.append(github_note.strip())
    if persona:
        parts.append("## أسلوبك مع المالك\nكلامك مع المالك (الرد النهائي والأسئلة) يكون بأسلوب هذه الهوية ولهجتها. "
                     "الهوية للأسلوب والشخصية فقط؛ قواعد العمل الهندسي فوق تبقى كما هي.\n"
                     f"<identity>\n{persona}\n</identity>")
    return "\n\n".join(parts)


# ================= الوصف والعرض =================

def describe(name: str, raw_args: str) -> str | None:
    a = llm.parse_args(raw_args) or {}
    p = str(a.get("path") or "")
    if name == "read_file":
        return f"📄 يقرأ {p}"
    if name == "write_file":
        return f"➕ يكمل {p}" if a.get("append") else f"✍️ يكتب {p}"
    if name == "edit_file":
        return f"✏️ يعدّل {p}"
    if name == "list_files":
        return f"📂 يستعرض {a.get('pattern') or 'الملفات'}"
    if name == "grep":
        return f"🔍 يدوّر «{str(a.get('pattern', ''))[:40]}»"
    if name == "run":
        cmd = " ".join(str(a.get("command", "")).split())
        return ("▶️ بالخلفية: " if a.get("background") else "▶️ ") + (cmd[:70] + ("…" if len(cmd) > 70 else ""))
    if name == "job":
        return f"📟 job {a.get('id', '')} {a.get('action') or 'output'}"
    if name == "todo_write":
        return "📋 يحدّث الخطة"
    if name == "dispatch_agents":
        tasks = a.get("tasks") if isinstance(a.get("tasks"), list) else []
        ids = "، ".join(str(t.get("id")) for t in tasks if isinstance(t, dict))[:80]
        return f"🤖 يوزّع {len(tasks)} مهام: {ids}" if tasks else "🤖 يوزّع مهام"
    if name == "ask_user":
        return "❓ يسألك"
    if name == "present_plan":
        return "📝 يقدّم الخطة"
    if name == "git_clone":
        return f"⬇️ يستنسخ {a.get('repo', '')}"
    if name == "git_push":
        return f"⬆️ يرفع على {a.get('repo', '')}:{a.get('branch') or 'main'}"
    return None


_ICON = {"waiting": "⏳", "running": "🟢", "done": "✅", "failed": "❌", "skipped": "⏭", "stopped": "⏹"}
_TODO = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}


def _mm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _l(text: str) -> str:
    return RLM + text if text.strip() else text


def files_summary(files: list[dict]) -> tuple[int, int, int]:
    plus = sum(f["plus"] or 0 for f in files)
    minus = sum(f["minus"] or 0 for f in files)
    return len(files), plus, minus


def _file_line(f: dict) -> str:
    sign = {"added": "+", "deleted": "−", "modified": "~"}[f["kind"]]
    stat = ""
    if f["plus"] is not None:
        stat = f" (+{f['plus']} −{f['minus']})" if f["kind"] == "modified" else \
            f" (+{f['plus']})" if f["kind"] == "added" else ""
    return f"{sign} <code>{esc(f['path'])}</code>{stat}"


def board_html(board: Board, final: bool, files: list[dict] | None = None, status: str = "",
               stopped: bool = False) -> str:
    """رسالة «لوحة العمل»: حيّة وهو يشتغل، وملخص قابل للطي في النهاية."""
    files = files or []
    agents = list(board.agents.values())
    if final and not (board.busy() or files):
        return ""
    todo_lines = [_l(f"{_TODO[t['status']]} " + (f"<b>{esc(t['content'])}</b>" if t["status"] == "in_progress"
                                                 else esc(t["content"]))) for t in board.todos[:15]]
    if len(board.todos) > 15:
        todo_lines.append(_l(f"<i>… و{len(board.todos) - 15} كمان</i>"))

    def agent_line(st: AgentState) -> str:
        head = f"{_ICON[st.status]} <code>{esc(st.id)}</code> · {esc(st.title)}"
        if st.status == "running" or (st.status == "waiting" and st.activity):
            return _l(head + (f" — {esc(st.activity)}" if st.activity else ""))
        extra = []
        if st.files:
            extra.append(f"{len(st.files)} ملف")
        if st.started:
            extra.append(_mm(st.seconds))
        return _l(head + (f" — {'، '.join(extra)}" if extra else ""))

    if not final:
        rows = [_l(f"🛠 <b>يشتغل…</b> · 📁 <code>{esc(board.project)}</code>")]
        if todo_lines:
            rows += ["", _l("📋 <b>الخطة</b>"), *todo_lines]
        if agents:
            running = sum(a.status == "running" for a in agents)
            fin = sum(a.status in ("done", "failed", "skipped", "stopped") for a in agents)
            rows += ["", _l(f"🤖 <b>الوكلاء</b> · {running} شغالين · {fin} خلصوا"), *map(agent_line, agents[-14:])]
        if status:
            rows += ["", _l(f"<i>{esc(status)}</i>")]
        return _cap("\n".join(rows))

    secs = time.monotonic() - board.started
    title = "⏹ <b>توقف الشغل" if stopped else "🛠 <b>خلص الشغل"
    head = [_l(f"{title} بعد {render.duration_ar(secs)}</b>")]
    stats = []
    if agents:
        stats.append(f"🤖 {len(agents)} وكلاء")
    if files:
        n, plus, minus = files_summary(files)
        stats.append(f"📝 {n} ملف (+{plus} −{minus})")
    if board.commands:
        stats.append(f"▶️ {len(board.commands)} أوامر")
    cost = board.usage.total()["cost"]
    if cost:
        stats.append(f"💰 ${cost:.3f}")
    if stats:
        head.append(_l(" · ".join(stats)))
    body = []
    if todo_lines:
        body += ["📋 <b>الخطة</b>", *todo_lines]
    if agents:
        body += ([""] if body else []) + ["🤖 <b>الوكلاء</b>", *map(agent_line, agents[:20])]
    if files:
        body += ([""] if body else []) + ["📝 <b>الملفات</b>", *[_l(_file_line(f)) for f in files[:25]]]
        if len(files) > 25:
            body.append(_l(f"<i>… و{len(files) - 25} ملف كمان</i>"))
    html = "\n".join(head)
    if body:
        inner = "\n".join(_l(b) if b and not b.startswith(RLM) else b for b in body)
        room = 3900 - len(html)
        if len(inner) > room:
            inner = inner[: max(0, room - 40)].rsplit("\n", 1)[0] + "\n" + _l("<i>…</i>")
        html += f"\n<blockquote expandable>{inner}</blockquote>"
    return html


def _cap(html: str, limit: int = 3900) -> str:
    if len(html) <= limit:
        return html
    return html[:limit].rsplit("\n", 1)[0] + "\n" + _l("<i>…</i>")


def work_log(board: Board, files: list[dict]) -> str:
    """ملخص الشغل ينحفظ مع رد القائد في ذاكرة المحادثة (عشان الطلب الجاي يعرف وش صار)."""
    lines = []
    if board.todos:
        lines.append("الخطة: " + " | ".join(f"{_TODO[t['status']]} {t['content']}" for t in board.todos[:15]))
    for st in list(board.agents.values())[:15]:
        first = " ".join(st.report.split())[:220]
        lines.append(f"وكيل {st.id} ({st.title}) [{st.status}]: {first}")
    if files:
        lines.append("الملفات: " + ", ".join(f"{ {'added': '+', 'deleted': '-', 'modified': '~'}[f['kind']]}{f['path']}"
                                            for f in files[:40]))
    if board.commands:
        lines.append("آخر الأوامر: " + " ; ".join(
            f"`{' '.join(c[1].split())[:80]}` → {'timeout' if c[3] else c[2]}" for c in board.commands[-8:]))
    return "<work_log>\n" + "\n".join(lines) + "\n</work_log>" if lines else ""


def reports_markdown(board: Board) -> str:
    out = [f"# تقارير الوكلاء — {board.project}\n"]
    for st in board.agents.values():
        out.append(f"## {_ICON[st.status]} {st.id} — {st.title}\n"
                   f"- الحالة: {st.status} · الجولات: {st.rounds} · المدة: {_mm(st.seconds)}\n"
                   f"- الملفات: {', '.join(sorted(st.files)) or '—'}\n\n{st.report or '—'}\n")
    return "\n".join(out)


def usage_line(u: dict) -> str:
    return f"{u['prompt'] + u['completion']:,} توكن ({u['calls']} طلب) · ${u['cost']:.4f}"


__all__ = ["Env", "Board", "ToolCtx", "AgentState", "Usage", "dispatch", "execute_lead", "lead_tools",
           "worker_tools", "lead_system", "board_html", "work_log", "describe", "trim_context", "TERMINAL",
           "reports_markdown", "usage_line", "files_summary", "human"]

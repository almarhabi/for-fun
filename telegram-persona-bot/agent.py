"""Coding mode: Kimi leads (plans and delegates), GLM agents execute in parallel inside a real workspace.

- Project tools (read/write/edit/search/commands) are shared by the lead and the agents.
- dispatch_agents: schedules tasks in parallel with dependencies (depends_on) and a parallelism limit.
- Each agent has a file scope and can't write inside another agent's scope (Claims), and can't overwrite a file
  it hasn't read or that changed since it read it — so parallel work never steps on itself.
- Board: the live state shown to the owner (plan + agents + commands + usage).
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
from render import esc
from workspace import Changes, Claims, Workspace, WorkspaceError, clip, human

log = logging.getLogger("persona-bot.agent")

# ================= Tool definitions =================

def _fn(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


T_READ = _fn("read_file", "Read a project file (line numbers are for display only). For long files use offset/limit.",
             {"path": {"type": "string", "description": "Path relative to the project root"},
              "offset": {"type": "integer", "description": "First line (1-based)"},
              "limit": {"type": "integer", "description": "Number of lines (default 2000)"}}, ["path"])
T_WRITE = _fn("write_file", "Write a whole file (creates it and its folders if missing). For an existing file: read it "
              "first. For huge files write the first part, then continue with append=true.",
              {"path": {"type": "string"}, "content": {"type": "string", "description": "The full content"},
               "append": {"type": "boolean", "description": "Append to the end of the file instead of overwriting"}},
              ["path", "content"])
T_EDIT = _fn("edit_file", "Edit a file by replacing existing text exactly (old_string must be unique in the file unless "
             "replace_all). For several edits to the same file send edits instead (all apply or none do).",
             {"path": {"type": "string"}, "old_string": {"type": "string", "description": "The current text, exactly"},
              "new_string": {"type": "string", "description": "The new text"},
              "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
              "edits": {"type": "array", "description": "Several edits, applied in order",
                        "items": {"type": "object", "properties": {
                            "old_string": {"type": "string"}, "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"}}, "required": ["old_string", "new_string"]}}},
             ["path"])
T_LIST = _fn("list_files", "List project files by glob pattern (e.g. **/*.py or src/**). Skips node_modules, .git and the like.",
             {"pattern": {"type": "string", "description": "Default **/*"},
              "path": {"type": "string", "description": "Starting folder (default: the root)"}})
T_GREP = _fn("grep", "Search project files with a regex. Returns file:line: text.",
             {"pattern": {"type": "string"}, "path": {"type": "string"},
              "glob": {"type": "string", "description": "Filter files, e.g. *.ts"},
              "ignore_case": {"type": "boolean"}}, ["pattern"])
T_RUN = _fn("run", "Run a bash command inside the project (install, build, test, run). Waits until it finishes or times "
            "out. For servers and never-ending processes use background=true. No interactive commands.",
            {"command": {"type": "string"},
             "timeout": {"type": "integer", "description": "Seconds (default 180)"},
             "cwd": {"type": "string", "description": "Sub-folder (default: the root)"},
             "background": {"type": "boolean", "description": "Run in the background and return a job id"}}, ["command"])
T_JOB = _fn("job", "Follow background commands: output (output and status), kill (stop) or list.",
            {"id": {"type": "integer"}, "action": {"type": "string", "enum": ["output", "kill", "list"]}})
T_TODO = _fn("todo_write", "Write/update the work plan (shown to the owner live). Send the whole list every time.",
             {"todos": {"type": "array", "items": {"type": "object", "properties": {
                 "content": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                 "required": ["content", "status"]}}}, ["todos"])
T_DISPATCH = _fn(
    "dispatch_agents",
    "Delegate tasks to worker agents that run in parallel (dependent ones run in order via depends_on). Waits until "
    "they finish and returns each agent's report and the files it changed. Agents don't see the conversation: put "
    "everything they need in instructions.",
    {"tasks": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "description": "Short id (e.g. api, db, tests)"},
        "title": {"type": "string", "description": "Short title shown to the owner"},
        "instructions": {"type": "string", "description": "The full task: goal, context, files, contracts, constraints, verification"},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Files/folders/patterns this agent owns and may write"},
        "depends_on": {"type": "array", "items": {"type": "string"},
                       "description": "Task ids that must finish first (their reports are passed along)"},
        "readonly": {"type": "boolean", "description": "Read/search/review only, no file edits"},
        "agent": {"type": "string", "description": "Agent type: coder (default) · explorer · reviewer · tester · "
                                                   "or a custom agent defined in the project (.agents/)"}},
        "required": ["id", "title", "instructions"]}}},
    ["tasks"])
T_ASK = _fn("ask_user", "Ask the owner a question (with button options) when an ambiguity would change the result "
            "fundamentally. Ends your current reply; the answer comes back as a message.",
            {"question": {"type": "string"},
             "options": {"type": "array", "items": {"type": "string"}, "description": "2-5 short options"}},
            ["question", "options"])
T_PLAN = _fn("present_plan", "Present the execution plan to the owner for approval (Markdown). Ends your current reply.",
             {"plan": {"type": "string"}}, ["plan"])
T_CLONE = _fn("git_clone", "Clone a GitHub repository into the project (into the root if the project is empty, "
              "otherwise into a sub-folder).",
              {"repo": {"type": "string", "description": "owner/name or name"},
               "branch": {"type": "string"}, "dest": {"type": "string"}}, ["repo"])
T_PUSH = _fn("git_push", "Push the whole project to GitHub (commit all changes, then push). Use only when the owner asks.",
             {"repo": {"type": "string", "description": "owner/name or name"},
              "branch": {"type": "string", "description": "Default main"},
              "message": {"type": "string", "description": "Commit message"},
              "create_if_missing": {"type": "boolean", "description": "Create the repository if it doesn't exist"},
              "private": {"type": "boolean", "description": "If created: private? (default yes)"},
              "force": {"type": "boolean", "description": "Force push — only if the owner explicitly asked"}},
             ["repo"])

WS_READ = [T_READ, T_LIST, T_GREP]
WS_ALL = [T_READ, T_WRITE, T_EDIT, T_LIST, T_GREP, T_RUN, T_JOB]
WS_NAMES = {t["function"]["name"] for t in WS_ALL}
TERMINAL = {"ask_user", "present_plan"}
GH_READ = {"gh_list_repos", "gh_read_file", "gh_list_dir"}


def lead_tools(plan: bool, web_tools: list, gh_tools: list, git: bool) -> list:
    if plan:  # plan mode: explore and read only
        return WS_READ + [T_TODO, T_ASK, T_PLAN] + web_tools + [t for t in gh_tools if t["function"]["name"] in GH_READ]
    return WS_ALL + [T_TODO, T_DISPATCH, T_ASK] + web_tools + gh_tools + ([T_CLONE, T_PUSH] if git else [])


def worker_tools(readonly: bool, web_tools: list) -> list:
    base = [T_READ, T_LIST, T_GREP, T_RUN, T_JOB] if readonly else list(WS_ALL)
    return base + web_tools


# ================= Agent types (like subagents in Claude Code) =================

BUILTIN_AGENTS = {
    "coder": {"description": "Builder: writes the code and tests it (default)", "readonly": False, "prompt": ""},
    "explorer": {"description": "Fast explorer: reads and searches, returns a precise summary with paths and lines — no edits",
                 "readonly": True,
                 "prompt": "Your role: explorer. Read and search efficiently (grep first), and return a precise summary: "
                           "paths, functions, how the requested part works, with line numbers. Do not modify any file."},
    "reviewer": {"description": "Strict code reviewer: logic bugs, edge cases and vulnerabilities — no edits",
                 "readonly": True,
                 "prompt": "Your role: strict code reviewer. Hunt for logic bugs, edge cases, races, security "
                           "vulnerabilities, and violations of the contracts and style. Run the tests if useful. Rank "
                           "findings by severity: file and line, the problem, and how to fix it. Skip cosmetic notes "
                           "unless they matter. Do not modify any file."},
    "tester": {"description": "Test writer: covers behavior and edge cases, and runs them",
               "readonly": False,
               "prompt": "Your role: test writer. Write tests covering the required behavior, edge cases and error "
                         "paths, using the project's test tooling, and run them. If a test fails because of a bug in "
                         "the code (not the test), don't change the code — describe it in detail in your report."},
}


def _front_matter(text: str) -> tuple[dict, str]:
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                k, _, v = line.partition(":")
                if k.strip():
                    meta[k.strip().lower()] = v.strip().strip("\"'")
            text = text[end + 4:]
    return meta, text.strip()


def custom_agents(ws: Workspace) -> dict:
    """Custom agents defined by the project: .agents/<name>.md or .claude/agents/<name>.md
    (optional front matter: description, readonly: true, model: ...)."""
    out = {}
    for folder in (".claude/agents", ".agents"):
        d = ws.root / folder
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            meta, body = _front_matter(f.read_text(encoding="utf-8", errors="replace"))
            name = _SLUG_NAME.sub("-", meta.get("name") or f.stem).strip("-")[:30]
            if name:
                out[name] = {"description": meta.get("description") or body.split("\n", 1)[0][:120],
                             "readonly": meta.get("readonly", "").lower() in ("true", "yes", "1"),
                             "model": meta.get("model") or None, "prompt": clip(body, 6000)}
    return out


_SLUG_NAME = re.compile(r"[^A-Za-z0-9_\-]+")


def agent_types(ws: Workspace) -> dict:
    return {**BUILTIN_AGENTS, **custom_agents(ws)}


def agent_types_text(ws: Workspace) -> str:
    return "\n".join(f"- {k}: {v['description']}" + (" (read-only)" if v.get("readonly") else "")
                     + (f" — model {v['model']}" if v.get("model") else "") for k, v in agent_types(ws).items())


# ================= Live state =================

@dataclass
class AgentState:
    id: str
    title: str
    readonly: bool = False
    deps: list = field(default_factory=list)
    files_scope: list = field(default_factory=list)
    kind: str = "coder"
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
    """Everything shown to the owner while it works: the plan, the agents, the commands, the usage."""

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


# ================= Run environment =================

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
    on_status: Callable[[str], None] | None = None   # the live status line (e.g. while watching CI)
    gh_login: Callable[[], Awaitable[str]] | None = None
    author: tuple = ("Kimi Agent", "agent@users.noreply.github.com")
    headers: dict = field(default_factory=dict)
    worker_prompt: str = ""
    # permission mode "ask": returns True if the owner approved (None = automatic, never asks)
    approve: Callable[[str, str], Awaitable[bool]] | None = None


class ToolCtx:
    """The context of one agent (or the lead): what it read, at which version, and its scope."""

    def __init__(self, env: Env, agent_id: str = "lead", readonly: bool = False,
                 claims: Claims | None = None, state: AgentState | None = None):
        self.env, self.agent_id, self.readonly, self.claims, self.state = env, agent_id, readonly, claims, state
        self.read_hashes: dict[str, str | None] = {}
        # token saver: an unchanged part of a file that was already read isn't sent again (reset when context is trimmed)
        self.seen_reads: dict[tuple, str | None] = {}


_DANGER = re.compile(r"(\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|/\*|~|~/|\$HOME|\.\.|\.\./?\S*)(\s|;|&|$))"
                     r"|\bmkfs\b|\b(shutdown|reboot|poweroff|halt)\b|:\(\)\s*\{|\bdd\s+[^|;]*of=/dev/"
                     r"|\bchmod\s+-R\s+\S+\s+/(\s|$)|>\s*/dev/sd")


# purely read-only commands need no approval even in "ask" mode (like Claude Code)
_SAFE_CMD = re.compile(r"^\s*(ls|pwd|cat|head|tail|wc|grep|rg|find|tree|echo|which|file|du|"
                       r"git\s+(status|diff|log|show|branch|remote\s+-v))\b[^;&|<>`$]*$")
DENIED = "⛔ The owner denied this action. Don't retry the same thing; try another way or explain why you need it."
HOOKS_FILE = ".agents/hooks.json"


async def _approved(ctx: "ToolCtx", what: str, detail: str) -> bool:
    if ctx.env.approve is None:
        return True
    who = "lead" if ctx.agent_id == "lead" else f"agent {ctx.agent_id}"
    return await ctx.env.approve(f"{what} — {who}", detail)


async def run_hooks(ctx: "ToolCtx", rel: str) -> str:
    """Project hooks (.agents/hooks.json): after_edit runs after every file edit — e.g. a formatter or linter.
    Any error or output goes back to the agent with the edit result, so it can fix things right away."""
    f = ctx.env.ws.root / HOOKS_FILE
    if not f.is_file():
        return ""
    try:
        cmd = json.loads(f.read_text(encoding="utf-8")).get("after_edit")
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(cmd, str) or not cmd.strip():
        return ""
    import shlex
    r = await ctx.env.ws.run(cmd.replace("{file}", shlex.quote(rel)), 90)
    if r["timed_out"] or r["code"] or r["output"].strip():
        status = "timeout" if r["timed_out"] else f"exit {r['code']}"
        return f"\n🪝 hook after_edit ({status}):\n{clip(r['output'], 2500)}"
    return ""


def _fmt_run(command: str, r: dict, timeout: float) -> str:
    status = f"⏱ timed out after {timeout:.0f}s and was stopped — if it's a server use background=true" if r["timed_out"] \
        else f"exit {r['code']}"
    return f"$ {command}\n{clip(r['output']) or '(no output)'}\n[{status} · {r['seconds']:.1f}s]"


async def exec_ws(ctx: ToolCtx, name: str, args: dict) -> str:
    """Runs a project tool and returns text for the model. Expected errors come back as text instead of crashing the agent."""
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
                    return (f"[{rel}: same content you already read in this session and it hasn't changed — "
                            "refer to that earlier result instead of re-reading]")
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
                return "⛔ Your task is read-only — you can't edit files. Put the suggested changes in your report."
            full = ws.resolve(args.get("path"))
            rel = ws.rel(full)
            if ctx.claims and (err := ctx.claims.check(ctx.agent_id, rel)):
                return "⛔ " + err
            if rel == HOOKS_FILE and not await _approved(ctx, "🪝 Change the project hooks", str(args.get("content", ""))[:800]):
                return DENIED
            if full.is_file():
                known, cur = ctx.read_hashes.get(rel, "∅"), ws.file_hash(full)
                if name == "write_file" and not args.get("append") and known == "∅":
                    return (f"⛔ {rel} already exists — read it with read_file before overwriting it "
                            "(or use edit_file for a precise change).")
                if known not in ("∅", cur):
                    return f"⛔ {rel} changed since you read it (another agent or a command) — read it again before editing."
            env.changes.note(full, ctx.agent_id)
            if name == "write_file":
                out = ws.write(args.get("path"), str(args.get("content", "")), bool(args.get("append")))
            elif isinstance(args.get("edits"), list):
                out = ws.edit_many(args.get("path"), args["edits"])
            else:
                out = ws.edit(args.get("path"), str(args.get("old_string", "")), str(args.get("new_string", "")),
                              bool(args.get("replace_all")))
            out += await run_hooks(ctx, rel)
            ctx.read_hashes[rel] = ws.file_hash(full)
            if ctx.state is not None:
                ctx.state.files.add(rel)
            return out
        if name == "run":
            command = str(args.get("command", "")).strip()
            if not command:
                return "⛔ Empty command"
            if _DANGER.search(command):
                return "⛔ Destructive command refused (deleting/damaging outside the project). Work inside the project folder only."
            if not _SAFE_CMD.match(command) and not await _approved(
                    ctx, "▶️ Run a command" + (" in the background" if args.get("background") else ""), command):
                return DENIED
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
        return f"⛔ Tool failed: {type(e).__name__}: {e}"
    return f"⛔ Unknown tool: {name}"


# ================= Worker agents =================

def _transient(e: Exception) -> bool:
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(e, APIStatusError) and e.status_code in (408, 409, 425, 429, 500, 502, 503, 504)


async def call_model(env: Env, messages: list, kw: dict, tool_kwargs: dict, on_delta=None,
                     on_retry: Callable[[str], None] | None = None) -> llm.Completion:
    """A model call with retries for transient errors (provider overload, dropped connection)."""
    for attempt in range(3):
        try:
            res = await llm.stream_chat(env.client, messages, kw, tool_kwargs, env.headers, on_delta)
            env.board.usage.add(kw["model"], res.usage)
            return res
        except Exception as e:
            if attempt == 2 or not _transient(e):
                raise
            if on_retry:
                on_retry(f"⏳ Provider busy — retry {attempt + 1}")
            await asyncio.sleep(4 * (attempt + 1) ** 2)
    raise RuntimeError("unreachable")


def trim_context(messages: list, limit: int = 240_000, target: int = 140_000, keep_last: int = 8) -> bool:
    """Saves tokens without losing understanding: once the session grows past `limit`, old tool results and the
    content of files written earlier (they're in the project anyway) are shortened until it's below `target`.
    Trimming happens in large, rare steps so the start of the conversation stays stable between rounds
    (better provider-side caching). Returns True if it trimmed."""
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
            m["content"] = m["content"][:250] + "\n…[old result shortened to save space — rerun the tool if you need it]"
        for c in m.get("tool_calls") or []:
            a = llm.parse_args(c["function"]["arguments"])
            if a and len(c["function"]["arguments"]) > 600:
                for k in ("content", "new_string", "old_string", "instructions"):
                    if isinstance(a.get(k), str) and len(a[k]) > 200:
                        a[k] = a[k][:120] + "…[shortened — the content is saved in the project]"
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
            approx = f"~{size / 1000:.0f}k chars" if size >= 1000 else ""
            if c["name"] in ("write_file", "edit_file") and m:
                st.activity = f"✍️ writing {m.group(1)}" + (f" ({approx})" if approx else "")
            elif c["name"]:
                st.activity = describe(c["name"], c["args"]) or f"🛠 {c['name']}"
        elif raw.strip():
            st.activity = "📝 writing the report…"
        elif reasoning:
            st.activity = "💭 thinking…"
    return on_delta


def env_block(env: Env) -> str:
    tools = [t for t in ("python3", "pip", "node", "npm", "git", "go", "cargo", "java", "gcc", "docker") if shutil.which(t)]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<env>\nProject: {env.ws.name} — the current folder is the project root; all paths are relative to it\n"
            f"Date: {now} · OS: {platform.system()} · Available tools: {', '.join(tools) or '—'}\n"
            f"Web search: {'available' if env.web else 'off'}\n</env>")


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
    parts = [f"Task id: {st.id}", f"Title: {st.title}"]
    if st.readonly:
        parts.append("⚠️ Read/review-only task: do not modify any file.")
    elif st.files_scope:
        parts.append("Your file scope (you write here): " + ", ".join(st.files_scope))
    if others:
        parts.append("Agents working in parallel with you (don't touch their files):\n" + "\n".join(
            f"- {o.id}: {o.title}" + (f" — {', '.join(o.files_scope)}" if o.files_scope else "") for o in others))
    parts.append("## Task\n" + instructions.strip())
    for d in deps:
        files = ", ".join(sorted(d.files)) or "—"
        parts.append(f"## Result of a task you depend on: {d.id} — {d.title}\n"
                     f"Files it changed: {files}\n{clip(d.report, 3500)}")
    return "\n\n".join(parts)


async def run_worker(env: Env, st: AgentState, brief: str, claims: Claims) -> None:
    ctx = ToolCtx(env, st.id, st.readonly, claims, st)
    tools = worker_tools(st.readonly, env.web_tools if env.web else [])
    spec = agent_types(env.ws).get(st.kind) or BUILTIN_AGENTS["coder"]
    system = worker_system(env) + (f"\n\n## Your role in this task ({st.kind})\n{spec['prompt']}" if spec.get("prompt") else "")
    kw = dict(env.worker_kw, model=spec["model"]) if spec.get("model") else env.worker_kw
    messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
    on_delta = _progress(st)

    def on_retry(msg: str) -> None:
        st.activity = msg

    for rnd in range(env.worker_rounds + 1):
        if rnd == env.worker_rounds:
            messages.append({"role": "user", "content": "⏱ You're out of rounds. Write your final report now, without any tools."})
            tk = {"tools": tools, "tool_choice": "none"}
        else:
            tk = {"tools": tools}
        if trim_context(messages, limit=200_000, target=120_000):
            ctx.seen_reads.clear()  # old results were shortened — the next read must come back in full
        st.activity = "💭 thinking…"
        res = await call_model(env, messages, kw, tk, on_delta, on_retry)
        st.rounds += 1
        if not res.calls:
            st.report = res.content.strip() or "(the agent finished without writing a report)"
            st.status = "done"
            st.activity = ""
            return
        messages.append(llm.assistant_message(res.content, res.calls, res.details))
        for c in res.calls:
            env.board.tool_calls += 1
            args = llm.parse_args(c["args"])
            st.activity = describe(c["name"], c["args"]) or f"🛠 {c['name']}"
            if args is None:
                result = ("⛔ The tool arguments aren't valid JSON (probably cut off because they were too long). "
                          "For a huge file: write a part with write_file, then continue with append=true.")
            elif c["name"] in WS_NAMES:
                result = await exec_ws(ctx, c["name"], args)
            elif env.web and c["name"] in {t["function"]["name"] for t in env.web_tools}:
                result, _ = await env.web(c["name"], c["args"])
            else:
                result = f"⛔ Tool not available to you: {c['name']}"
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
    """Runs a batch of tasks: independent ones in parallel (up to max_parallel), dependent ones after their dependencies."""
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return "⛔ tasks must be a list of tasks"
    if len(raw_tasks) > env.max_tasks:
        return f"⛔ At most {env.max_tasks} tasks per batch — merge small tasks or split them across batches"
    board = env.board
    types = agent_types(env.ws)
    rename: dict[str, str] = {}
    specs = []
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            return f"⛔ Task #{i + 1} is not an object"
        raw_id = str(t.get("id") or f"t{i + 1}")
        tid = _SLUG.sub("-", raw_id).strip("-")[:24] or f"t{i + 1}"
        base, n = tid, 2
        while tid in board.agents or tid in rename.values():
            tid, n = f"{base}-{n}", n + 1
        rename[raw_id] = tid
        instructions = str(t.get("instructions") or "").strip()
        if not instructions:
            return f"⛔ Task \"{raw_id}\" has no instructions — agents don't see the conversation; write the full task"
        files = [str(f) for f in (t.get("files") or []) if isinstance(f, str)][:40]
        deps = [str(d) for d in (t.get("depends_on") or []) if isinstance(d, (str, int))]
        kind = str(t.get("agent") or "coder").strip() or "coder"
        if kind not in types:
            return f"⛔ Unknown agent type \"{kind}\". Available: {', '.join(types)}"
        ro = bool(t.get("readonly")) or bool(types[kind].get("readonly"))
        specs.append((tid, str(t.get("title") or raw_id)[:80], instructions, files, deps, ro, kind))
    graph = {}
    for tid, _, _, _, deps, _, _ in specs:
        unknown = [d for d in deps if d not in rename]
        if unknown:
            return f"⛔ depends_on has ids that aren't in this batch: {', '.join(unknown)}"
        graph[tid] = [rename[d] for d in deps]
    if cyc := _cycle(graph):
        return f"⛔ Circular dependency between tasks: {' → '.join(cyc)}"

    board.batches += 1
    states: dict[str, AgentState] = {}
    instr: dict[str, str] = {}
    claims = Claims()
    for tid, title, instructions, files, _, ro, kind in specs:
        st = AgentState(tid, title, ro, graph[tid], files, kind)
        states[tid] = board.agents[tid] = st
        instr[tid] = instructions
        if not ro:
            claims.register(tid, files)
    done = {tid: asyncio.Event() for tid in states}
    sem = asyncio.Semaphore(max(1, env.max_parallel))

    async def run_one(st: AgentState) -> None:
        try:
            if st.deps:
                st.activity = "⏳ waiting for " + ", ".join(st.deps)
                for d in st.deps:
                    await done[d].wait()
                bad = [d for d in st.deps if states[d].status != "done"]
                if bad:
                    st.status = "skipped"
                    st.report = f"Skipped because the tasks it depends on didn't succeed: {', '.join(bad)}"
                    st.activity = ""
                    return
            st.activity = "⏳ queued"
            async with sem:
                st.status, st.started, st.activity = "running", time.monotonic(), "💭 reading the task…"
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
                    st.report = st.report or f"Agent failed: {st.error}"
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
    note = (f"{ok}/{len(results)} succeeded. Review the reports, read the important files, and run verification "
            "before treating it as done." if ok else
            "No task succeeded — look at the reasons and decide: fix it yourself, or re-delegate with clearer instructions.")
    return json.dumps({"results": results, "note": note}, ensure_ascii=False)


# ================= Lead tools =================

async def resolve_repo(env: Env, repo: str) -> str:
    repo = (repo or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if "/" in repo or not env.gh_login:
        return repo
    return f"{await env.gh_login()}/{repo}"


async def git_tool(env: Env, name: str, args: dict) -> str:
    if not env.gh_token:
        return "⛔ GitHub is not connected — the owner needs to add the GH_PAT secret."
    try:
        repo = await resolve_repo(env, str(args.get("repo", "")))
        if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo):
            return "⛔ The repository name must be owner/name"
        if name == "git_clone":
            dest = args.get("dest") or ("." if env.ws.is_empty() else repo.split("/")[-1])
            return await env.ws.clone(repo, args.get("branch"), env.gh_token, dest)
        branch = str(args.get("branch") or "main")
        message = str(args.get("message") or "Update from Telegram agent")
        try:
            res = await env.ws.push(repo, branch, message, env.gh_token, env.author, bool(args.get("force")))
        except WorkspaceError as e:
            if not (args.get("create_if_missing") and env.gh and "doesn't exist" in str(e)):
                raise
            created = await env.gh("gh_create_repo", json.dumps({
                "name": repo.split("/")[-1], "private": args.get("private", True), "auto_init": False}))
            if "failed" in created.lower():
                return f"⛔ Couldn't create the repository: {created}"
            res = await env.ws.push(repo, branch, message, env.gh_token, env.author, False)
            res["created_repo"] = True
        return json.dumps(res, ensure_ascii=False)
    except WorkspaceError as e:
        return f"⛔ {e}"


_GH_WRITE_TOOLS = {"gh_put_file", "gh_delete_file", "gh_create_repo", "gh_create_branch", "gh_open_pr"}
_GH_WRITE_ACTIONS = {"gh_pr": {"comment", "merge", "update_branch", "close"},
                     "gh_issue": {"create", "update", "comment"},
                     "gh_actions": {"rerun", "cancel", "dispatch"},
                     "gh_repo": {"release", "delete_branch"}}


def is_gh_write(name: str, args: dict) -> bool:
    return name in _GH_WRITE_TOOLS or args.get("action") in _GH_WRITE_ACTIONS.get(name, set())


async def execute_lead(env: Env, ctx: ToolCtx, name: str, raw_args: str) -> tuple[str, list]:
    """The lead's tools (except ask_user and present_plan, which end the reply and are handled by the bot)."""
    args = llm.parse_args(raw_args)
    if args is None:
        return ("⛔ The tool arguments aren't valid JSON (probably cut off because they were too long). For a huge "
                "file: write a part with write_file, then continue with append=true. For long tasks: split them across "
                "two batches."), []
    if name == "todo_write":
        n = env.board.set_todos(args.get("todos"))
        return f"Plan updated ({n} tasks).", []
    if name == "dispatch_agents":
        return await dispatch(env, args.get("tasks")), []
    if name in WS_NAMES:
        return await exec_ws(ctx, name, args), []
    if name in ("git_clone", "git_push"):
        if name == "git_push" and not await _approved(
                ctx, "⬆️ Push to GitHub", f"{args.get('repo')} ← {args.get('branch') or 'main'}"
                                         + (" (force)" if args.get("force") else "")):
            return DENIED, []
        return await git_tool(env, name, args), []
    if env.web and name in {t["function"]["name"] for t in env.web_tools}:
        return await env.web(name, raw_args)
    if env.gh and name.startswith("gh_"):
        if is_gh_write(name, args) and not await _approved(
                ctx, "🐙 Change something on GitHub", (describe(name, raw_args) or name) + "\n" + json.dumps(args, ensure_ascii=False)[:600]):
            return DENIED, []
        return await env.gh(name, raw_args, progress=env.on_status), []
    return f"⛔ Unknown tool: {name}", []


def lead_system(env: Env, base_prompt: str, plan: bool, persona: str | None, github_note: str,
                journal: str = "") -> str:
    parts = [base_prompt]
    if plan:
        parts.append(
            "## ⚠️ Plan mode is on\n"
            "The owner wants to see and approve the plan before anything is executed. Right now: explore and understand "
            "(read-only — you can't write, run commands or dispatch agents), then present a detailed execution plan with "
            "`present_plan`: your understanding of the request and your assumptions, the architecture and design, how "
            "the work splits across agents (who does what, in which order, and which files), and how it will be "
            "verified. If the request is just a question that needs no execution, answer it directly.")
    parts.append(env_block(env).replace(
        "</env>", f"Worker agents: {env.worker_kw.get('model')} — up to {env.max_parallel} in parallel, "
                  f"up to {env.max_tasks} tasks per batch\nGitHub: {'connected' if env.gh_token else 'not connected'}\n</env>"))
    parts.append(f"<agent_types>\nAgent types available to dispatch_agents (the agent field):\n{agent_types_text(env.ws)}\n</agent_types>")
    parts.append(f"<project_tree>\n{env.ws.tree(200)}\n</project_tree>")
    if mem := project_memory(env.ws):
        parts.append(mem)
    if journal:
        parts.append("<project_journal>\nA short log of every previous request in this project (oldest first). "
                     "The current code in the files is the source of truth; this log is context only.\n"
                     f"{journal}\n</project_journal>")
    if github_note and env.gh_token:
        parts.append(github_note.strip())
    if persona:
        parts.append("## Your voice with the owner\nWhen you talk to the owner (the final reply and questions), use this "
                     "identity's style and voice. The identity sets style and personality only; the engineering rules "
                     "above stay exactly as they are.\n"
                     f"<identity>\n{persona}\n</identity>")
    return "\n\n".join(parts)


# ================= Descriptions and rendering =================

def describe(name: str, raw_args: str) -> str | None:
    a = llm.parse_args(raw_args) or {}
    p = str(a.get("path") or "")
    if name == "read_file":
        return f"📄 Reading {p}"
    if name == "write_file":
        return f"➕ Appending to {p}" if a.get("append") else f"✍️ Writing {p}"
    if name == "edit_file":
        return f"✏️ Editing {p}"
    if name == "list_files":
        return f"📂 Listing {a.get('pattern') or 'files'}"
    if name == "grep":
        return f"🔍 Searching \"{str(a.get('pattern', ''))[:40]}\""
    if name == "run":
        cmd = " ".join(str(a.get("command", "")).split())
        return ("▶️ Background: " if a.get("background") else "▶️ ") + (cmd[:70] + ("…" if len(cmd) > 70 else ""))
    if name == "job":
        return f"📟 job {a.get('id', '')} {a.get('action') or 'output'}"
    if name == "todo_write":
        return "📋 Updating the plan"
    if name == "dispatch_agents":
        tasks = a.get("tasks") if isinstance(a.get("tasks"), list) else []
        ids = ", ".join(str(t.get("id")) for t in tasks if isinstance(t, dict))[:80]
        return f"🤖 Delegating {len(tasks)} tasks: {ids}" if tasks else "🤖 Delegating tasks"
    if name == "ask_user":
        return "❓ Asking you"
    if name == "present_plan":
        return "📝 Presenting the plan"
    if name == "git_clone":
        return f"⬇️ Cloning {a.get('repo', '')}"
    if name == "git_push":
        return f"⬆️ Pushing to {a.get('repo', '')}:{a.get('branch') or 'main'}"
    return None


_ICON = {"waiting": "⏳", "running": "🟢", "done": "✅", "failed": "❌", "skipped": "⏭", "stopped": "⏹"}
_TODO = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}


def _mm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _l(text: str) -> str:
    return text  # English UI: left-to-right (Arabic content still lays itself out right-to-left)


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
    """The "work panel" message: live while working, and a collapsible summary at the end."""
    files = files or []
    agents = list(board.agents.values())
    if final and not (board.busy() or files):
        return ""
    todo_lines = [_l(f"{_TODO[t['status']]} " + (f"<b>{esc(t['content'])}</b>" if t["status"] == "in_progress"
                                                 else esc(t["content"]))) for t in board.todos[:15]]
    if len(board.todos) > 15:
        todo_lines.append(_l(f"<i>… and {len(board.todos) - 15} more</i>"))

    def agent_line(st: AgentState) -> str:
        head = f"{_ICON[st.status]} <code>{esc(st.id)}</code> · {esc(st.title)}"
        if st.status == "running" or (st.status == "waiting" and st.activity):
            return _l(head + (f" — {esc(st.activity)}" if st.activity else ""))
        extra = []
        if st.files:
            extra.append(f"{len(st.files)} file" + ("s" if len(st.files) != 1 else ""))
        if st.started:
            extra.append(_mm(st.seconds))
        return _l(head + (f" — {', '.join(extra)}" if extra else ""))

    if not final:
        rows = [_l(f"🛠 <b>Working…</b> · 📁 <code>{esc(board.project)}</code>")]
        if todo_lines:
            rows += ["", _l("📋 <b>Plan</b>"), *todo_lines]
        if agents:
            running = sum(a.status == "running" for a in agents)
            fin = sum(a.status in ("done", "failed", "skipped", "stopped") for a in agents)
            rows += ["", _l(f"🤖 <b>Agents</b> · {running} running · {fin} finished"), *map(agent_line, agents[-14:])]
        if status:
            rows += ["", _l(f"<i>{esc(status)}</i>")]
        return _cap("\n".join(rows))

    secs = time.monotonic() - board.started
    title = "⏹ <b>Stopped" if stopped else "🛠 <b>Done"
    head = [_l(f"{title} after {render.duration_text(secs)}</b>")]
    stats = []
    if agents:
        stats.append(f"🤖 {len(agents)} agent" + ("s" if len(agents) != 1 else ""))
    if files:
        n, plus, minus = files_summary(files)
        stats.append(f"📝 {n} file" + ("s" if n != 1 else "") + f" (+{plus} −{minus})")
    if board.commands:
        stats.append(f"▶️ {len(board.commands)} command" + ("s" if len(board.commands) != 1 else ""))
    cost = board.usage.total()["cost"]
    if cost:
        stats.append(f"💰 ${cost:.3f}")
    if stats:
        head.append(_l(" · ".join(stats)))
    body = []
    if todo_lines:
        body += ["📋 <b>Plan</b>", *todo_lines]
    if agents:
        body += ([""] if body else []) + ["🤖 <b>Agents</b>", *map(agent_line, agents[:20])]
    if files:
        body += ([""] if body else []) + ["📝 <b>Files</b>", *[_l(_file_line(f)) for f in files[:25]]]
        if len(files) > 25:
            body.append(_l(f"<i>… and {len(files) - 25} more files</i>"))
    html = "\n".join(head)
    if body:
        inner = "\n".join(body)
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
    """A summary of the work, saved with the lead's reply in conversation memory (so the next request knows what happened)."""
    lines = []
    if board.todos:
        lines.append("Plan: " + " | ".join(f"{_TODO[t['status']]} {t['content']}" for t in board.todos[:15]))
    for st in list(board.agents.values())[:15]:
        first = " ".join(st.report.split())[:220]
        lines.append(f"Agent {st.id} ({st.title}) [{st.status}]: {first}")
    if files:
        lines.append("Files: " + ", ".join(f"{ {'added': '+', 'deleted': '-', 'modified': '~'}[f['kind']]}{f['path']}"
                                          for f in files[:40]))
    if board.commands:
        lines.append("Last commands: " + " ; ".join(
            f"`{' '.join(c[1].split())[:80]}` → {'timeout' if c[3] else c[2]}" for c in board.commands[-8:]))
    return "<work_log>\n" + "\n".join(lines) + "\n</work_log>" if lines else ""


def reports_markdown(board: Board) -> str:
    out = [f"# Agent reports — {board.project}\n"]
    for st in board.agents.values():
        out.append(f"## {_ICON[st.status]} {st.id} — {st.title} ({st.kind})\n"
                   f"- Status: {st.status} · Rounds: {st.rounds} · Time: {_mm(st.seconds)}\n"
                   f"- Files: {', '.join(sorted(st.files)) or '—'}\n\n{st.report or '—'}\n")
    return "\n".join(out)


def usage_line(u: dict) -> str:
    return f"{u['prompt'] + u['completion']:,} tokens ({u['calls']} requests) · ${u['cost']:.4f}"


__all__ = ["Env", "Board", "ToolCtx", "AgentState", "Usage", "dispatch", "execute_lead", "lead_tools",
           "worker_tools", "lead_system", "board_html", "work_log", "describe", "trim_context", "TERMINAL",
           "reports_markdown", "usage_line", "files_summary", "human"]

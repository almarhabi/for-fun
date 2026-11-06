"""The coding-mode workspace — the project folder Kimi and its agents work in (like Claude Code).

Every project is its own folder under data/workspaces/<name>:
- File tools (read/write/edit/search) can never reach outside the project folder.
- Commands run inside the folder with a scrubbed environment: tokens and keys never reach them.
- Every file-tool edit saves the original first → "undo" and "diff" (checkpoints).
- Files are claimed between parallel agents so two agents never write the same file.
- Git: clone and push to GitHub with a token the model never sees and that is never stored in the project.
"""

import asyncio
import base64
import contextlib
import difflib
import hashlib
import io
import itertools
import json
import os
import re
import signal
import time
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

# Heavy dependency/output folders: hidden from listings and search, and left out of zips
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".next", ".nuxt", ".cache", ".tox", ".gradle", ".idea", ".turbo",
             ".parcel-cache", ".svelte-kit", "target"}
NAME_RE = re.compile(r"[\w][\w.\-]{0,47}")
MAX_READ_CHARS = 60_000      # most text returned by a single file read
OUT_LIMIT = 16_000           # most command output returned to the model
MAX_JOBS = 5                 # background commands running at the same time
GIT_BASE = os.getenv("GITHUB_GIT_BASE", "https://github.com").rstrip("/")

_SENSITIVE = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH|"
                        r"(^|_)PAT($|_)|(^|_)KEY($|_)", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")


class WorkspaceError(Exception):
    """An expected error that goes back to the model as text (not a bot failure)."""


# ================= Helpers =================

def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name) or ".." in name:
        raise WorkspaceError("A project name can only use letters, digits and - _ . (up to 48 characters, no spaces)")
    return name


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def clean_output(text: str) -> str:
    """Removes terminal colors and progress bars (\\r) so the output is clean for the model."""
    text = _ANSI.sub("", text.replace("\r\n", "\n"))
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def clip(text: str, limit: int = OUT_LIMIT) -> str:
    """Cuts the middle out of long text: the start and the end matter most (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n… [cut {len(text) - limit:,} characters from the middle] …\n{text[-tail:]}"


def glob_re(pattern: str) -> re.Pattern:
    """glob to regex, supporting ** and {a,b}."""
    p = pattern.strip()
    if p.startswith("./"):
        p = p[2:]
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        elif p[i] == "{" and (j := p.find("}", i)) > i:
            out += "(?:" + "|".join(re.escape(x) for x in p[i + 1 : j].split(",")) + ")"
            i = j + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(out + r"\Z")


def glob_match(pattern: str, rel: str) -> bool:
    """A pattern without / matches the file name anywhere (like *.py); with / it matches the whole path."""
    target = rel if "/" in pattern.strip().removeprefix("./") else PurePosixPath(rel).name
    return bool(glob_re(pattern).match(target))


def in_scope(rel: str, patterns: list[str]) -> bool:
    for p in patterns:
        p = p.strip().removeprefix("./").strip("/")
        if not p:
            continue
        if any(ch in p for ch in "*?{"):
            if glob_match(p, rel):
                return True
        elif rel == p or rel.startswith(p + "/"):
            return True
    return False


def count_lines(data: bytes | None) -> int | None:
    if data is None or is_binary(data):
        return None
    text = data.decode("utf-8", "replace")
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def line_delta(old: bytes | str, new: bytes | str) -> tuple[int | None, int | None]:
    """How many lines were added and removed (like git diff --stat)."""
    if isinstance(old, bytes):
        if is_binary(old) or is_binary(new):
            return None, None
        old, new = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
    a, b = old.splitlines(), new.splitlines()
    if len(a) + len(b) > 40_000:
        return max(0, len(b) - len(a)), max(0, len(a) - len(b))
    plus = minus = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            minus += i2 - i1
        if tag in ("replace", "insert"):
            plus += j2 - j1
    return plus, minus


def safe_env(extra: dict | None = None, ceiling: str | None = None) -> dict:
    """The environment for commands: the machine's environment without any token/key/password."""
    # inherited GIT_CONFIG_* may carry auth headers — drop them all (dropping only some makes git reject the config)
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE.search(k) and not k.startswith("GIT_CONFIG_")}
    env.update(CI="1", GIT_TERMINAL_PROMPT="0", PYTHONUNBUFFERED="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               NO_COLOR="1", DEBIAN_FRONTEND="noninteractive", PAGER="cat", GIT_PAGER="cat",
               npm_config_yes="true", PYTHONDONTWRITEBYTECODE="1")
    if ceiling:  # git never climbs above the projects folder (projects live inside the bot's own repository)
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    if extra:
        env.update(extra)
    return env


def _kill(proc) -> None:
    """Stops the command and everything it started (the whole process group)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


class _Buf:
    """Collects a command's output: keeps only the start and the end if it's huge."""
    HEAD, TAIL = 64_000, 400_000

    def __init__(self):
        self.head, self.tail, self.dropped = bytearray(), bytearray(), 0

    def add(self, b: bytes) -> None:
        if len(self.head) < self.HEAD:
            take = self.HEAD - len(self.head)
            self.head += b[:take]
            b = b[take:]
        if b:
            self.tail += b
            if len(self.tail) > self.TAIL:
                cut = len(self.tail) - self.TAIL
                self.dropped += cut
                del self.tail[:cut]

    def text(self) -> str:
        if self.dropped:
            return (clean_output(self.head.decode("utf-8", "replace")) + f"\n… [{self.dropped:,} bytes not kept] …\n"
                    + clean_output(self.tail.decode("utf-8", "replace")))
        return clean_output(bytes(self.head + self.tail).decode("utf-8", "replace"))


async def _pump(stream, buf: _Buf) -> None:
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        buf.add(chunk)


class Job:
    def __init__(self, jid: int, command: str, proc, buf: _Buf, reader: asyncio.Task):
        self.id, self.command, self.proc, self.buf, self.reader = jid, command, proc, buf, reader
        self.started = time.time()
        self.waiter = asyncio.ensure_future(proc.wait())

    @property
    def running(self) -> bool:
        return self.proc.returncode is None

    def status(self) -> str:
        secs = int(time.time() - self.started)
        return f"running ({secs}s)" if self.running else f"finished, exit {self.proc.returncode}"


_jobs: dict[str, dict[int, Job]] = {}
_job_seq = itertools.count(1)


def kill_all_jobs() -> None:
    for jobs in _jobs.values():
        for j in jobs.values():
            if j.running:
                _kill(j.proc)


# ================= The project =================

class Workspace:
    def __init__(self, base: Path, name: str):
        self.name = valid_name(name)
        self.base = base
        self.root = base / self.name

    def ensure(self) -> "Workspace":
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def real(self) -> Path:
        return self.root.resolve()

    def is_empty(self) -> bool:
        return not self.root.is_dir() or not any(self.root.iterdir())

    def resolve(self, path: str | None) -> Path:
        raw = (path or ".").strip() or "."
        if raw.startswith("~"):
            raise WorkspaceError("Use a path inside the project (relative, like src/app.py)")
        p = Path(raw)
        root = self.real
        full = (p if p.is_absolute() else root / p).resolve()
        if full != root and root not in full.parents:
            raise WorkspaceError(f"The path \"{raw}\" is outside the project folder — every path must be inside the project")
        return full

    def rel(self, full: Path) -> str:
        r = full.resolve().relative_to(self.real).as_posix()
        return r or "."

    def walk(self, start: Path | None = None):
        for dirpath, dirnames, filenames in os.walk(start or self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                yield Path(dirpath) / f

    def stats(self) -> tuple[int, int]:
        n = size = 0
        if self.root.is_dir():
            for f in self.walk():
                n += 1
                with contextlib.suppress(OSError):
                    size += f.stat().st_size
        return n, size

    # ----- Reading -----

    def read(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        full = self.resolve(path)
        if full.is_dir():
            raise WorkspaceError(f"\"{path}\" is a folder — use list_files")
        if not full.is_file():
            raise WorkspaceError(f"The file \"{path}\" doesn't exist")
        rel = self.rel(full)
        size = full.stat().st_size
        if size > 25_000_000:
            raise WorkspaceError(f"The file is huge ({human(size)}) — use grep or run (head/sed) instead of reading it")
        data = full.read_bytes()
        if is_binary(data):
            return f"[{rel}: binary file, {human(size)} — can't be read as text]"
        text = data.decode("utf-8", "replace")
        if not text:
            return f"[{rel}: the file is empty]"
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        total = len(lines)
        offset = max(1, int(offset or 1))
        limit = max(1, min(int(limit or 2000), 5000))
        if offset > total:
            return f"[{rel}: has only {total} lines — offset {offset} is past the end of the file]"
        out, used = [], 0
        for i, line in enumerate(lines[offset - 1 : offset - 1 + limit], offset):
            if len(line) > 2000:
                line = line[:2000] + " …[long line cut]"
            row = f"{i:>6}\t{line}"
            if used + len(row) > MAX_READ_CHARS and out:
                break
            out.append(row)
            used += len(row) + 1
        end = offset + len(out) - 1
        head = f"[{rel} — {total} lines"
        if offset > 1 or end < total:
            head += f", showing {offset}-{end}. For the rest use offset={end + 1}"
        return head + "]\n" + "\n".join(out)

    def list_files(self, pattern: str = "**/*", path: str = ".", limit: int = 400) -> str:
        start = self.resolve(path)
        if not start.is_dir():
            raise WorkspaceError(f"\"{path}\" is not a folder")
        pattern = (pattern or "**/*").strip() or "**/*"
        hits = []
        for f in self.walk(start):
            if glob_match(pattern, f.relative_to(start).as_posix()):
                hits.append(f)
        if not hits:
            return f"No files match \"{pattern}\" in {self.rel(start)}"
        rows = []
        for f in hits[:limit]:
            with contextlib.suppress(OSError):
                rows.append(f"{self.rel(f)}  ({human(f.stat().st_size)})")
        more = f"\n… and {len(hits) - limit} more files — narrow the pattern" if len(hits) > limit else ""
        return f"[{len(hits)} files]\n" + "\n".join(rows) + more

    def grep(self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False,
             limit: int = 150) -> str:
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise WorkspaceError(f"Invalid regex: {e}") from None
        start = self.resolve(path)
        files = [start] if start.is_file() else self.walk(start)
        out, nfiles = [], 0
        for f in files:
            rel = self.rel(f)
            if glob and not glob_match(glob, rel):
                continue
            try:
                if f.stat().st_size > 3_000_000:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if is_binary(data):
                continue
            hit = False
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel}:{i}: {line.strip()[:240]}")
                    hit = True
                    if len(out) >= limit:
                        break
            nfiles += hit
            if len(out) >= limit:
                out.append(f"… stopped at {limit} results — narrow the search")
                break
        if not out:
            return f"No matches for \"{pattern}\""
        return f"[{nfiles} files]\n" + "\n".join(out)

    def tree(self, max_entries: int = 200) -> str:
        if self.is_empty():
            return "(the project is empty)"
        lines: list[str] = []
        more = 0

        def walk(d: Path, prefix: str) -> None:
            nonlocal more
            try:
                entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for e in entries:
                if len(lines) >= max_entries:
                    more += 1
                    continue
                if e.is_dir() and not e.is_symlink():
                    if e.name in SKIP_DIRS:
                        lines.append(f"{prefix}{e.name}/ …")
                        continue
                    lines.append(f"{prefix}{e.name}/")
                    walk(e, prefix + "  ")
                else:
                    lines.append(f"{prefix}{e.name}")

        walk(self.root, "")
        if more:
            lines.append(f"… (+{more} more)")
        return "\n".join(lines)

    # ----- Writing -----

    def write(self, path: str, content: str, append: bool = False) -> str:
        full = self.resolve(path)
        if full == self.real or full.is_dir():
            raise WorkspaceError(f"\"{path}\" is a folder — give a file path")
        full.parent.mkdir(parents=True, exist_ok=True)
        existed = full.is_file()
        if append:
            with open(full, "a", encoding="utf-8", newline="") as f:
                f.write(content)
        else:
            full.write_text(content, encoding="utf-8", newline="")
        n = count_lines(full.read_bytes()) or 0
        verb = "Appended to" if append else ("Overwrote" if existed else "Created")
        return f"✓ {verb} {self.rel(full)} ({n} lines now)"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        full = self.resolve(path)
        if not full.is_file():
            raise WorkspaceError(f"The file \"{path}\" doesn't exist — to create a file use write_file")
        if not old:
            raise WorkspaceError("old_string is empty — to write a whole file use write_file")
        if old == new:
            raise WorkspaceError("old_string and new_string are identical — nothing to change")
        text = full.read_bytes().decode("utf-8", "surrogateescape")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")  # a file with Windows line endings
            count = text.count(old)
        if count == 0:
            hint = ""
            if re.search(r"^\s*\d+\t", old, re.M):
                hint = " — don't copy the line numbers that read_file shows"
            elif old.strip() and old.strip() in text:
                hint = " — there is a whitespace or blank-line difference around it"
            raise WorkspaceError(f"old_string not found in {self.rel(full)}{hint}. Read the file and copy the text exactly.")
        if count > 1 and not replace_all:
            raise WorkspaceError(f"old_string occurs {count} times in {self.rel(full)} — add surrounding context to make "
                                 "it unique, or use replace_all=true to change all of them")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        full.write_bytes(updated.encode("utf-8", "surrogateescape"))
        plus, minus = line_delta(text, updated)
        where = f"{count} places" if replace_all else "1 place"
        return f"✓ Edited {self.rel(full)} ({where}, +{plus} −{minus} lines)"

    def edit_many(self, path: str, edits: list) -> str:
        """Several edits to one file in one go (like MultiEdit): either all apply or nothing is written."""
        full = self.resolve(path)
        if not full.is_file():
            raise WorkspaceError(f"The file \"{path}\" doesn't exist — to create a file use write_file")
        if not isinstance(edits, list) or not edits:
            raise WorkspaceError("edits must be a list of edits")
        original = text = full.read_bytes().decode("utf-8", "surrogateescape")
        for i, e in enumerate(edits, 1):
            old, new = str(e.get("old_string", "")), str(e.get("new_string", ""))
            if not old or old == new:
                raise WorkspaceError(f"Edit {i}: old_string is empty or identical to new_string — nothing was written")
            count = text.count(old)
            if count == 0:
                raise WorkspaceError(f"Edit {i}: old_string not found (after applying the earlier edits) — nothing was written")
            if count > 1 and not e.get("replace_all"):
                raise WorkspaceError(f"Edit {i}: old_string occurs {count} times — add context — nothing was written")
            text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
        full.write_bytes(text.encode("utf-8", "surrogateescape"))
        plus, minus = line_delta(original, text)
        return f"✓ Edited {self.rel(full)} ({len(edits)} edits, +{plus} −{minus} lines)"

    def file_hash(self, full: Path) -> str | None:
        try:
            return hashlib.sha1(full.read_bytes()).hexdigest()
        except OSError:
            return None

    # ----- Commands -----

    async def _spawn(self, command: str, cwd: Path):
        return await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=str(cwd), env=safe_env(ceiling=str(self.base.resolve())),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def run(self, command: str, timeout: float = 180, cwd: str = ".") -> dict:
        """Runs a bash command inside the project and waits for it. Returns {code, output, seconds, timed_out}."""
        workdir = self.resolve(cwd)
        if not workdir.is_dir():
            raise WorkspaceError(f"The folder \"{cwd}\" doesn't exist")
        t0 = time.monotonic()
        proc = await self._spawn(command, workdir)
        buf = _Buf()
        reader = asyncio.create_task(_pump(proc.stdout, buf))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            _kill(proc)
            reader.cancel()
            raise
        finally:
            _kill(proc)  # clean up anything the command started that is still hanging around
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        try:
            await asyncio.wait_for(reader, 3)
        except (asyncio.TimeoutError, Exception):
            reader.cancel()
        return {"code": None if timed_out else proc.returncode, "output": buf.text(),
                "seconds": time.monotonic() - t0, "timed_out": timed_out}

    async def start_job(self, command: str, cwd: str = ".") -> str:
        workdir = self.resolve(cwd)
        jobs = _jobs.setdefault(str(self.real), {})
        if sum(j.running for j in jobs.values()) >= MAX_JOBS:
            raise WorkspaceError(f"{MAX_JOBS} background commands are already running — stop one with job(action=kill) first")
        proc = await self._spawn(command, workdir)
        buf = _Buf()
        job = Job(next(_job_seq), command, proc, buf, asyncio.create_task(_pump(proc.stdout, buf)))
        jobs[job.id] = job
        await asyncio.wait({job.waiter}, timeout=3)  # give it a few seconds to print something (or fail fast)
        return (f"▶️ job {job.id} in the background — {job.status()}\n{clip(buf.text(), 4000) or '(no output yet)'}\n"
                "Output: job(id, action=output) · Stop: job(id, action=kill)")

    def job(self, jid: int | None = None, action: str = "output") -> str:
        jobs = _jobs.get(str(self.real), {})
        if action == "list" or jid is None:
            if not jobs:
                return "No background commands"
            return "\n".join(f"job {j.id}: {j.status()} — {j.command[:120]}" for j in jobs.values())
        j = jobs.get(int(jid))
        if not j:
            raise WorkspaceError(f"No job number {jid}")
        if action == "kill":
            _kill(j.proc)
            return f"⏹ Stopped job {j.id}"
        return f"job {j.id} — {j.status()}\n$ {j.command}\n{clip(j.buf.text(), 8000) or '(no output)'}"

    # ----- Uploads and downloads -----

    def save_upload(self, filename: str, data: bytes) -> str:
        """Saves a file the owner sent. A zip is extracted automatically (into the root if the project is empty)."""
        name = re.sub(r"[\\/:\x00]", "_", PurePosixPath(filename or "file").name).strip(". ") or "file"
        if name.lower().endswith(".zip"):
            dest = "." if self.is_empty() else f"uploads/{name[:-4]}"
            n = self.extract_zip(data, dest)
            return f"{n} files extracted from {name} into {dest}/"
        full = self.resolve(f"uploads/{name}")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return f"uploads/{name} ({human(len(data))})"

    def extract_zip(self, data: bytes, dest: str = ".") -> int:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WorkspaceError("Corrupted zip file") from None
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) > 5000 or sum(m.file_size for m in members) > 300_000_000:
            raise WorkspaceError("The archive is too big (limit 5000 files / 300MB)")
        names = [m.filename for m in members]
        tops = {n.split("/", 1)[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)  # a single wrapping folder → drop it
        target = self.resolve(dest)
        n = 0
        for m in members:
            name = m.filename.split("/", 1)[1] if strip else m.filename
            if not name or name.startswith("__MACOSX"):
                continue
            out = (target / name).resolve()
            if self.real not in out.parents:  # zip-slip
                continue
            if (m.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(m))
            n += 1
        return n

    def zip_bytes(self, limit: int = 45_000_000) -> bytes:
        buf = io.BytesIO()
        raw = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in self.walk():
                with contextlib.suppress(OSError):
                    raw += f.stat().st_size
                    if raw > limit * 4:
                        raise WorkspaceError("The project is too big for Telegram — push it to GitHub instead")
                    z.write(f, arcname=f"{self.name}/{self.rel(f)}")
        data = buf.getvalue()
        if len(data) > limit:
            raise WorkspaceError(f"The zip is {human(len(data))} — bigger than Telegram's limit")
        return data

    # ----- Git -----

    async def git(self, args: list[str], token: str = "", cwd: Path | None = None, timeout: float = 300) -> tuple[int, str]:
        extra = {}
        if token:  # the token goes in a header for this command only — never in the URL or in .git/config
            b = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            extra = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{GIT_BASE}/.extraheader",
                     "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {b}"}
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd or self.real), env=safe_env(extra, ceiling=str(self.base.resolve())),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            _kill(proc)
            return 124, "timed out"
        except asyncio.CancelledError:
            _kill(proc)
            raise
        text = clean_output(out.decode("utf-8", "replace"))
        if token:
            text = text.replace(token, "***")
        return proc.returncode or 0, text.strip()

    @staticmethod
    def repo_url(full: str) -> str:
        return f"{GIT_BASE}/{full}.git"

    async def clone(self, full: str, branch: str | None, token: str, dest: str = ".") -> str:
        self.ensure()
        target = self.resolve(dest)
        if target.exists() and any(target.iterdir()):
            raise WorkspaceError(f"The folder \"{dest}\" isn't empty — clone into a sub-folder (dest) or a new project")
        args = ["clone", "--depth", "50"] + (["--branch", branch] if branch else []) + [self.repo_url(full), str(target)]
        code, out = await self.git(args, token, cwd=self.real, timeout=600)
        if code:
            raise WorkspaceError(f"Clone failed: {out[-800:]}")
        n = sum(1 for _ in self.walk(target))
        return f"✓ Cloned {full}{'@' + branch if branch else ''} into {self.rel(target)}/ ({n} files)"

    def _excludes(self) -> None:
        info = self.real / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        f = info / "exclude"
        cur = f.read_text(encoding="utf-8") if f.exists() else ""
        want = ["node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".env", ".DS_Store",
                ".pytest_cache/", "uploads/"]
        missing = [w for w in want if w not in cur.split("\n")]
        if missing:
            f.write_text(cur.rstrip("\n") + ("\n" if cur.strip() else "") + "\n".join(missing) + "\n", encoding="utf-8")

    async def push(self, full: str, branch: str, message: str, token: str,
                   author: tuple[str, str] = ("Kimi Agent", "agent@users.noreply.github.com"),
                   force: bool = False) -> dict:
        root = self.ensure().real
        if not (root / ".git").exists():
            code, out = await self.git(["init", "-q", "-b", branch])
            if code:
                raise WorkspaceError(f"git init failed: {out}")
        self._excludes()
        code, out = await self.git(["add", "-A"])
        if code:
            raise WorkspaceError(f"git add failed: {out[-600:]}")
        code, out = await self.git(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "-c", "commit.gpgsign=false",
                                    "commit", "-q", "-m", message or "Update from Telegram agent"])
        committed = code == 0
        if code and "nothing" not in out:
            raise WorkspaceError(f"git commit failed: {out[-600:]}")
        code, sha = await self.git(["rev-parse", "HEAD"])
        if code:
            raise WorkspaceError("Nothing to push — the project is empty")
        args = ["push"] + (["--force"] if force else []) + [self.repo_url(full), f"HEAD:refs/heads/{branch}"]
        code, out = await self.git(args, token, timeout=600)
        if code:
            hint = ""
            low = out.lower()
            if "rejected" in low or "fetch first" in low or "non-fast-forward" in low:
                hint = " — the branch on GitHub has commits you don't have: push to a new branch, or clone it first"
            elif "not found" in low or "repository" in low and "exist" in low:
                hint = " — the repository doesn't exist (create it first) or the token lacks permission"
            raise WorkspaceError(f"git push failed{hint}: {out[-700:]}")
        url = f"{GIT_BASE}/{full}/tree/{branch}" if GIT_BASE.startswith("http") else f"{full}@{branch}"
        return {"ok": True, "repo": full, "branch": branch, "commit": sha[:10], "new_commit": committed, "url": url}


# ================= Checkpoints (undo / diff) =================

_BIG = "__too_big__"


class Changes:
    """Saves the original of every file before its first edit during one request — for undo, diff and the summary.

    Like Claude Code: it tracks edits made by the file tools. Changes made by commands (rm, mv, npm…) aren't tracked.
    """
    MAX_FILE = 2_000_000

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.originals: dict[str, bytes | str | None] = {}
        self.by_agent: dict[str, set] = defaultdict(set)

    def note(self, full: Path, agent: str = "lead") -> str:
        rel = self.ws.rel(full)
        if rel not in self.originals:
            if full.is_file():
                size = full.stat().st_size
                self.originals[rel] = full.read_bytes() if size <= self.MAX_FILE else _BIG
            else:
                self.originals[rel] = None
        self.by_agent[agent].add(rel)
        return rel

    def _current(self, rel: str) -> bytes | None:
        f = self.ws.root / rel
        return f.read_bytes() if f.is_file() else None

    def files(self) -> list[dict]:
        out = []
        for rel, orig in sorted(self.originals.items()):
            cur = self._current(rel)
            if orig == _BIG:
                out.append({"path": rel, "kind": "modified", "plus": None, "minus": None})
                continue
            if orig == cur:
                continue
            if orig is None:
                out.append({"path": rel, "kind": "added", "plus": count_lines(cur), "minus": 0})
            elif cur is None:
                out.append({"path": rel, "kind": "deleted", "plus": 0, "minus": count_lines(orig)})
            else:
                plus, minus = line_delta(orig, cur)
                out.append({"path": rel, "kind": "modified", "plus": plus, "minus": minus})
        return out

    def diff(self) -> str:
        parts = []
        for f in self.files():
            rel = f["path"]
            orig, cur = self.originals[rel], self._current(rel)
            if orig == _BIG or (orig and is_binary(orig)) or (cur and is_binary(cur)):
                parts.append(f"Binary/large file changed: {rel}\n")
                continue
            a = (orig or b"").decode("utf-8", "replace").splitlines(keepends=True)
            b = (cur or b"").decode("utf-8", "replace").splitlines(keepends=True)
            parts.append("".join(difflib.unified_diff(a, b, "/dev/null" if orig is None else f"a/{rel}",
                                                      "/dev/null" if cur is None else f"b/{rel}")))
        return "\n".join(p if p.endswith("\n") else p + "\n" for p in parts)

    def restore(self) -> tuple[int, list[str]]:
        restored, skipped = 0, []
        for rel, orig in self.originals.items():
            full = self.ws.root / rel
            if orig == _BIG:
                skipped.append(rel)
                continue
            cur = self._current(rel)
            if orig is None:
                if cur is not None:
                    full.unlink()
                    restored += 1
            elif cur != orig:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(orig)
                restored += 1
        return restored, skipped

    def to_json(self) -> dict:
        return {rel: (None if v is None else _BIG if v == _BIG else base64.b64encode(v).decode())
                for rel, v in self.originals.items()}

    @classmethod
    def from_json(cls, ws: Workspace, data: dict) -> "Changes":
        c = cls(ws)
        for rel, v in (data or {}).items():
            c.originals[rel] = None if v is None else _BIG if v == _BIG else base64.b64decode(v)
        return c


class Checkpoints:
    """The latest checkpoints of each project (saved to disk so they survive restarts)."""
    KEEP = 10
    MAX_BYTES = 30_000_000

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def push(self, project: str, cp_id: str, label: str, changes: Changes) -> None:
        data = self._load()
        stack = data.setdefault(project, [])
        stack.append({"id": cp_id, "time": int(time.time()), "label": label[:120], "files": changes.to_json()})
        del stack[: -self.KEEP]
        while len(json.dumps(data)) > self.MAX_BYTES and any(len(s) > 1 for s in data.values()):
            biggest = max(data.values(), key=len)
            biggest.pop(0)
        self._save(data)

    def peek(self, project: str) -> dict | None:
        stack = self._load().get(project) or []
        return stack[-1] if stack else None

    def pop(self, project: str, cp_id: str | None = None) -> dict | None:
        data = self._load()
        stack = data.get(project) or []
        if not stack or (cp_id and stack[-1]["id"] != cp_id):
            return None
        cp = stack.pop()
        self._save(data)
        return cp

    def count(self, project: str) -> int:
        return len(self._load().get(project) or [])

    def drop_project(self, project: str) -> None:
        data = self._load()
        if data.pop(project, None) is not None:
            self._save(data)


class Claims:
    """File claims between parallel agents: each agent has a scope, and the first to write a file claims it."""

    def __init__(self):
        self.owner: dict[str, str] = {}
        self.scopes: dict[str, list[str]] = {}

    def register(self, agent: str, patterns: list[str]) -> None:
        self.scopes[agent] = [p for p in patterns if isinstance(p, str) and p.strip()]

    def release(self, agent: str) -> None:
        self.scopes.pop(agent, None)
        self.owner = {k: v for k, v in self.owner.items() if v != agent}

    def check(self, agent: str, rel: str) -> str | None:
        holder = self.owner.get(rel)
        if holder and holder != agent:
            return (f"The file {rel} is being worked on by agent \"{holder}\" right now — don't edit it. "
                    "If it needs a change, describe it in your report to the lead.")
        if not in_scope(rel, self.scopes.get(agent) or []):
            for other, pats in self.scopes.items():
                if other != agent and in_scope(rel, pats):
                    return (f"The file {rel} is in agent \"{other}\"'s scope — don't edit it. "
                            "Describe the needed change in your report to the lead.")
        self.owner[rel] = agent
        return None

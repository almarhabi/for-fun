"""مساحة العمل لوضع الوكلاء — مجلد المشروع اللي يشتغل فيه Kimi ووكلاؤه (مثل Claude Code).

كل مشروع مجلد مستقل تحت data/workspaces/<الاسم>:
- أدوات الملفات (قراءة/كتابة/تعديل/بحث) ما تقدر تطلع برا مجلد المشروع.
- الأوامر تشتغل داخل المجلد بمتغيرات بيئة منظّفة: التوكنات والمفاتيح ما توصل لها أبداً.
- كل تعديل بأدوات الملفات ينحفظ أصله قبل التعديل → «تراجع» و«الفرق» (نقاط رجوع).
- حجز الملفات بين الوكلاء المتوازيين عشان ما يكتب اثنين على نفس الملف.
- Git: استنساخ ورفع على GitHub بتوكن ما يشوفه الموديل ولا ينحفظ داخل المشروع.
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

# مجلدات تبعيات/مخرجات ثقيلة: ما تنعرض في القوائم والبحث، وما تنضغط في zip
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".next", ".nuxt", ".cache", ".tox", ".gradle", ".idea", ".turbo",
             ".parcel-cache", ".svelte-kit", "target"}
NAME_RE = re.compile(r"[\w][\w.\-]{0,47}")
MAX_READ_CHARS = 60_000      # أقصى نص يرجع من قراءة ملف وحدة
OUT_LIMIT = 16_000           # أقصى مخرجات أمر ترجع للموديل
MAX_JOBS = 5                 # أوامر شغالة بالخلفية بنفس الوقت
GIT_BASE = os.getenv("GITHUB_GIT_BASE", "https://github.com").rstrip("/")

_SENSITIVE = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|COOKIE|AUTH|"
                        r"(^|_)PAT($|_)|(^|_)KEY($|_)", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")


class WorkspaceError(Exception):
    """خطأ مفهوم يرجع للموديل كنص (مو عطل في البوت)."""


# ================= مساعدات =================

def valid_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name) or ".." in name:
        raise WorkspaceError("اسم المشروع لازم يكون حروف/أرقام و - _ . بس (حتى 48 حرف، بدون مسافات)")
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
    """يشيل ألوان الطرفية وأشرطة التقدّم (\\r) عشان المخرجات تكون نظيفة للموديل."""
    text = _ANSI.sub("", text.replace("\r\n", "\n"))
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def clip(text: str, limit: int = OUT_LIMIT) -> str:
    """يقص من النص الطويل الوسط: البداية والنهاية أهم (الأخطاء غالباً بالنهاية)."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n… [قطعت {len(text) - limit:,} حرف من الوسط] …\n{text[-tail:]}"


def glob_re(pattern: str) -> re.Pattern:
    """glob إلى regex مع دعم ** و {a,b}."""
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
    """النمط بدون / يطابق اسم الملف بأي مكان (مثل *.py)، ومع / يطابق المسار كامل."""
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
    """كم سطر انضاف وكم انشال (مثل git diff --stat)."""
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
    """بيئة الأوامر: نفس بيئة الجهاز بدون أي توكن/مفتاح/كلمة سر."""
    # GIT_CONFIG_* الموروثة ممكن تحمل ترويسات توثيق — نشيلها كلها (لو شلنا بعضها git يرفض الإعداد الناقص)
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE.search(k) and not k.startswith("GIT_CONFIG_")}
    env.update(CI="1", GIT_TERMINAL_PROMPT="0", PYTHONUNBUFFERED="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               NO_COLOR="1", DEBIAN_FRONTEND="noninteractive", PAGER="cat", GIT_PAGER="cat",
               npm_config_yes="true", PYTHONDONTWRITEBYTECODE="1")
    if ceiling:  # git ما يطلع فوق مجلد المشاريع (المشروع داخل مستودع البوت نفسه)
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    if extra:
        env.update(extra)
    return env


def _kill(proc) -> None:
    """يوقف الأمر وكل اللي شغّله (مجموعة العمليات كاملة)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


class _Buf:
    """يجمع مخرجات أمر: يحفظ البداية والنهاية بس لو كانت ضخمة."""
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
            return (clean_output(self.head.decode("utf-8", "replace")) + f"\n… [{self.dropped:,} بايت ما انحفظت] …\n"
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
        return f"شغال ({secs}ث)" if self.running else f"انتهى exit {self.proc.returncode}"


_jobs: dict[str, dict[int, Job]] = {}
_job_seq = itertools.count(1)


def kill_all_jobs() -> None:
    for jobs in _jobs.values():
        for j in jobs.values():
            if j.running:
                _kill(j.proc)


# ================= المشروع =================

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
            raise WorkspaceError("استخدم مسار داخل المشروع (نسبي مثل src/app.py)")
        p = Path(raw)
        root = self.real
        full = (p if p.is_absolute() else root / p).resolve()
        if full != root and root not in full.parents:
            raise WorkspaceError(f"المسار «{raw}» برا مجلد المشروع — كل المسارات لازم تكون داخل المشروع")
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

    # ----- قراءة -----

    def read(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        full = self.resolve(path)
        if full.is_dir():
            raise WorkspaceError(f"«{path}» مجلد — استخدم list_files")
        if not full.is_file():
            raise WorkspaceError(f"الملف «{path}» مو موجود")
        rel = self.rel(full)
        size = full.stat().st_size
        if size > 25_000_000:
            raise WorkspaceError(f"الملف ضخم ({human(size)}) — استخدم grep أو run (head/sed) بدل القراءة")
        data = full.read_bytes()
        if is_binary(data):
            return f"[{rel}: ملف ثنائي {human(size)} — ما ينقرا كنص]"
        text = data.decode("utf-8", "replace")
        if not text:
            return f"[{rel}: الملف فاضي]"
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        total = len(lines)
        offset = max(1, int(offset or 1))
        limit = max(1, min(int(limit or 2000), 5000))
        if offset > total:
            return f"[{rel}: فيه {total} سطر بس — offset {offset} أكبر من طول الملف]"
        out, used = [], 0
        for i, line in enumerate(lines[offset - 1 : offset - 1 + limit], offset):
            if len(line) > 2000:
                line = line[:2000] + " …[سطر طويل مقصوص]"
            row = f"{i:>6}\t{line}"
            if used + len(row) > MAX_READ_CHARS and out:
                break
            out.append(row)
            used += len(row) + 1
        end = offset + len(out) - 1
        head = f"[{rel} — {total} سطر"
        if offset > 1 or end < total:
            head += f"، المعروض {offset}-{end}. للباقي استخدم offset={end + 1}"
        return head + "]\n" + "\n".join(out)

    def list_files(self, pattern: str = "**/*", path: str = ".", limit: int = 400) -> str:
        start = self.resolve(path)
        if not start.is_dir():
            raise WorkspaceError(f"«{path}» مو مجلد")
        pattern = (pattern or "**/*").strip() or "**/*"
        hits = []
        for f in self.walk(start):
            if glob_match(pattern, f.relative_to(start).as_posix()):
                hits.append(f)
        if not hits:
            return f"ما فيه ملفات تطابق «{pattern}» في {self.rel(start)}"
        rows = []
        for f in hits[:limit]:
            with contextlib.suppress(OSError):
                rows.append(f"{self.rel(f)}  ({human(f.stat().st_size)})")
        more = f"\n… و{len(hits) - limit} ملف كمان — ضيّق النمط" if len(hits) > limit else ""
        return f"[{len(hits)} ملف]\n" + "\n".join(rows) + more

    def grep(self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False,
             limit: int = 150) -> str:
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise WorkspaceError(f"regex غير صالح: {e}") from None
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
                out.append(f"… وقفت عند {limit} نتيجة — ضيّق البحث")
                break
        if not out:
            return f"ما فيه نتائج لـ «{pattern}»"
        return f"[{nfiles} ملف]\n" + "\n".join(out)

    def tree(self, max_entries: int = 200) -> str:
        if self.is_empty():
            return "(المشروع فاضي)"
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
            lines.append(f"… (+{more} عنصر)")
        return "\n".join(lines)

    # ----- كتابة -----

    def write(self, path: str, content: str, append: bool = False) -> str:
        full = self.resolve(path)
        if full == self.real or full.is_dir():
            raise WorkspaceError(f"«{path}» مجلد — حدّد مسار ملف")
        full.parent.mkdir(parents=True, exist_ok=True)
        existed = full.is_file()
        if append:
            with open(full, "a", encoding="utf-8", newline="") as f:
                f.write(content)
        else:
            full.write_text(content, encoding="utf-8", newline="")
        n = count_lines(full.read_bytes()) or 0
        verb = "أُضيف إلى" if append else ("انكتب فوق" if existed else "انكتب ملف جديد")
        return f"✓ {verb} {self.rel(full)} ({n} سطر الحين)"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        full = self.resolve(path)
        if not full.is_file():
            raise WorkspaceError(f"الملف «{path}» مو موجود — لإنشاء ملف استخدم write_file")
        if not old:
            raise WorkspaceError("old_string فاضي — لكتابة ملف كامل استخدم write_file")
        if old == new:
            raise WorkspaceError("old_string و new_string متطابقين — ما فيه تغيير")
        text = full.read_bytes().decode("utf-8", "surrogateescape")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")  # ملف بنهايات ويندوز
            count = text.count(old)
        if count == 0:
            hint = ""
            if re.search(r"^\s*\d+\t", old, re.M):
                hint = " — لا تنسخ أرقام الأسطر اللي يعرضها read_file"
            elif old.strip() and old.strip() in text:
                hint = " — فيه فرق بالمسافات أو الأسطر الفاضية حوله"
            raise WorkspaceError(f"ما لقيت old_string في {self.rel(full)}{hint}. اقرأ الملف وانسخ النص بالحرف.")
        if count > 1 and not replace_all:
            raise WorkspaceError(f"old_string موجود {count} مرات في {self.rel(full)} — زِد سياق حوله ليصير فريد، "
                                 "أو استخدم replace_all=true لو تبي تغيّرها كلها")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        full.write_bytes(updated.encode("utf-8", "surrogateescape"))
        plus, minus = line_delta(text, updated)
        where = f"{count} موضع" if replace_all else "موضع واحد"
        return f"✓ عُدّل {self.rel(full)} ({where}، +{plus} −{minus} سطر)"

    def file_hash(self, full: Path) -> str | None:
        try:
            return hashlib.sha1(full.read_bytes()).hexdigest()
        except OSError:
            return None

    # ----- الأوامر -----

    async def _spawn(self, command: str, cwd: Path):
        return await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=str(cwd), env=safe_env(ceiling=str(self.base.resolve())),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def run(self, command: str, timeout: float = 180, cwd: str = ".") -> dict:
        """يشغّل أمر bash داخل المشروع وينتظره. يرجع {code, output, seconds, timed_out}."""
        workdir = self.resolve(cwd)
        if not workdir.is_dir():
            raise WorkspaceError(f"المجلد «{cwd}» مو موجود")
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
            _kill(proc)  # ينظّف أي شي شغّله الأمر وبقى معلّق
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
            raise WorkspaceError(f"فيه {MAX_JOBS} أوامر شغالة بالخلفية — أوقف واحد بـ job(action=kill) أول")
        proc = await self._spawn(command, workdir)
        buf = _Buf()
        job = Job(next(_job_seq), command, proc, buf, asyncio.create_task(_pump(proc.stdout, buf)))
        jobs[job.id] = job
        await asyncio.wait({job.waiter}, timeout=3)  # نعطيه ثواني يطلع أول مخرجاته (أو يفشل بسرعة)
        return (f"▶️ job {job.id} بالخلفية — {job.status()}\n{clip(buf.text(), 4000) or '(ما فيه مخرجات بعد)'}\n"
                "للمخرجات: job(id, action=output) · للإيقاف: job(id, action=kill)")

    def job(self, jid: int | None = None, action: str = "output") -> str:
        jobs = _jobs.get(str(self.real), {})
        if action == "list" or jid is None:
            if not jobs:
                return "ما فيه أوامر بالخلفية"
            return "\n".join(f"job {j.id}: {j.status()} — {j.command[:120]}" for j in jobs.values())
        j = jobs.get(int(jid))
        if not j:
            raise WorkspaceError(f"ما فيه job رقم {jid}")
        if action == "kill":
            _kill(j.proc)
            return f"⏹ أوقفت job {j.id}"
        return f"job {j.id} — {j.status()}\n$ {j.command}\n{clip(j.buf.text(), 8000) or '(ما فيه مخرجات)'}"

    # ----- الملفات المرفوعة والتحميل -----

    def save_upload(self, filename: str, data: bytes) -> str:
        """يحفظ ملف أرسله المالك. zip ينفك تلقائياً (للجذر لو المشروع فاضي)."""
        name = re.sub(r"[\\/:\x00]", "_", PurePosixPath(filename or "file").name).strip(". ") or "file"
        if name.lower().endswith(".zip"):
            dest = "." if self.is_empty() else f"uploads/{name[:-4]}"
            n = self.extract_zip(data, dest)
            return f"{n} ملف انفكّت من {name} إلى {dest}/"
        full = self.resolve(f"uploads/{name}")
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return f"uploads/{name} ({human(len(data))})"

    def extract_zip(self, data: bytes, dest: str = ".") -> int:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WorkspaceError("ملف zip تالف") from None
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) > 5000 or sum(m.file_size for m in members) > 300_000_000:
            raise WorkspaceError("الأرشيف كبير جداً (حد 5000 ملف / 300MB)")
        names = [m.filename for m in members]
        tops = {n.split("/", 1)[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)  # مجلد واحد يغلّف الكل → نشيله
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
                        raise WorkspaceError("المشروع كبير على تليجرام — ارفعه على GitHub بدل كذا")
                    z.write(f, arcname=f"{self.name}/{self.rel(f)}")
        data = buf.getvalue()
        if len(data) > limit:
            raise WorkspaceError(f"الملف المضغوط {human(len(data))} — أكبر من حد تليجرام")
        return data

    # ----- Git -----

    async def git(self, args: list[str], token: str = "", cwd: Path | None = None, timeout: float = 300) -> tuple[int, str]:
        extra = {}
        if token:  # التوكن في ترويسة لهذا الأمر فقط — ما ينكتب في الرابط ولا في .git/config
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
            return 124, "انتهت المهلة"
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
            raise WorkspaceError(f"المجلد «{dest}» مو فاضي — استنسخ في مجلد فرعي (dest) أو مشروع جديد")
        args = ["clone", "--depth", "50"] + (["--branch", branch] if branch else []) + [self.repo_url(full), str(target)]
        code, out = await self.git(args, token, cwd=self.real, timeout=600)
        if code:
            raise WorkspaceError(f"فشل الاستنساخ: {out[-800:]}")
        n = sum(1 for _ in self.walk(target))
        return f"✓ استنسخت {full}{'@' + branch if branch else ''} إلى {self.rel(target)}/ ({n} ملف)"

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
                raise WorkspaceError(f"git init فشل: {out}")
        self._excludes()
        code, out = await self.git(["add", "-A"])
        if code:
            raise WorkspaceError(f"git add فشل: {out[-600:]}")
        code, out = await self.git(["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "-c", "commit.gpgsign=false",
                                    "commit", "-q", "-m", message or "Update from Telegram agent"])
        committed = code == 0
        if code and "nothing" not in out:
            raise WorkspaceError(f"git commit فشل: {out[-600:]}")
        code, sha = await self.git(["rev-parse", "HEAD"])
        if code:
            raise WorkspaceError("ما فيه شي ينرفع — المشروع فاضي")
        args = ["push"] + (["--force"] if force else []) + [self.repo_url(full), f"HEAD:refs/heads/{branch}"]
        code, out = await self.git(args, token, timeout=600)
        if code:
            hint = ""
            low = out.lower()
            if "rejected" in low or "fetch first" in low or "non-fast-forward" in low:
                hint = " — الفرع على GitHub فيه commits مو عندك: ارفع على فرع جديد، أو استنسخه أول"
            elif "not found" in low or "repository" in low and "exist" in low:
                hint = " — المستودع مو موجود (أنشئه أول) أو التوكن ما عنده صلاحية"
            raise WorkspaceError(f"git push فشل{hint}: {out[-700:]}")
        url = f"{GIT_BASE}/{full}/tree/{branch}" if GIT_BASE.startswith("http") else f"{full}@{branch}"
        return {"ok": True, "repo": full, "branch": branch, "commit": sha[:10], "new_commit": committed, "url": url}


# ================= نقاط الرجوع (تراجع / فرق) =================

_BIG = "__too_big__"


class Changes:
    """يحفظ أصل كل ملف قبل أول تعديل عليه خلال طلب واحد — للتراجع والفرق والملخّص.

    مثل Claude Code: يتتبّع تعديلات أدوات الملفات. تغييرات الأوامر (rm, mv, npm…) ما تنحسب.
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
    """آخر نقاط الرجوع لكل مشروع (تنحفظ على القرص عشان تبقى بعد إعادة التشغيل)."""
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
    """حجز الملفات بين الوكلاء المتوازيين: كل وكيل له نطاق، وأول من يكتب ملف يحجزه."""

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
            return (f"الملف {rel} يشتغل عليه الوكيل «{holder}» الحين — لا تعدّله. "
                    "لو يحتاج تغيير، اذكره في تقريرك للقائد.")
        if not in_scope(rel, self.scopes.get(agent) or []):
            for other, pats in self.scopes.items():
                if other != agent and in_scope(rel, pats):
                    return (f"الملف {rel} ضمن نطاق الوكيل «{other}» — لا تعدّله. "
                            "اذكر التغيير المطلوب في تقريرك للقائد.")
        self.owner[rel] = agent
        return None

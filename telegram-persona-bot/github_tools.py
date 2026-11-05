"""أدوات GitHub للبوت — للمالك فقط. يقرأ ويكتب وينشئ على مستودعات المالك مباشرة.

التوكن من السر GITHUB_PAT (أو GH_PAT). ما فيه حذف مستودعات (إجراء خطير وغير قابل للتراجع).
البوت يتأكد إن الطالب هو المالك قبل ما يشغّل أي أداة هنا (الفحص في bot.py).
"""

import base64
import json
import os

import httpx

API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
MAX_READ_CHARS = 50_000

GITHUB_TOOLS = [
    {"type": "function", "function": {
        "name": "gh_list_repos",
        "description": "اعرض مستودعات المالك على GitHub (الأحدث تعديلاً). استخدمها لتعرف أسماء المستودعات.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "فلترة بالاسم (اختياري)"}}}}},
    {"type": "function", "function": {
        "name": "gh_read_file",
        "description": "اقرأ محتوى ملف من مستودع.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "owner/name أو name"},
            "path": {"type": "string"},
            "ref": {"type": "string", "description": "فرع أو commit (اختياري)"}},
            "required": ["repo", "path"]}}},
    {"type": "function", "function": {
        "name": "gh_list_dir",
        "description": "اعرض محتويات مجلد في مستودع (أو الجذر لو path فاضي).",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string"}, "path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["repo"]}}},
    {"type": "function", "function": {
        "name": "gh_put_file",
        "description": "أنشئ ملفاً جديداً أو عدّل ملفاً موجوداً (يكتب المحتوى كامل ويعمل commit).",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string", "description": "محتوى الملف كامل"},
            "message": {"type": "string", "description": "رسالة الـ commit"},
            "branch": {"type": "string", "description": "الفرع (اختياري، الافتراضي الفرع الرئيسي)"}},
            "required": ["repo", "path", "content", "message"]}}},
    {"type": "function", "function": {
        "name": "gh_delete_file",
        "description": "احذف ملفاً من مستودع (يعمل commit).",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string"}, "path": {"type": "string"},
            "message": {"type": "string"}, "branch": {"type": "string"}},
            "required": ["repo", "path", "message"]}}},
    {"type": "function", "function": {
        "name": "gh_create_repo",
        "description": "أنشئ مستودعاً جديداً للمالك.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "private": {"type": "boolean", "description": "خاص؟ الافتراضي نعم"},
            "description": {"type": "string"},
            "auto_init": {"type": "boolean", "description": "ينشئ README وفرع أول؟ الافتراضي نعم"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "gh_create_branch",
        "description": "أنشئ فرعاً جديداً من فرع موجود.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string"}, "branch": {"type": "string"},
            "from_branch": {"type": "string", "description": "الفرع المصدر (اختياري)"}},
            "required": ["repo", "branch"]}}},
    {"type": "function", "function": {
        "name": "gh_open_pr",
        "description": "افتح Pull Request.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string"}, "title": {"type": "string"},
            "head": {"type": "string", "description": "الفرع اللي فيه التغييرات"},
            "base": {"type": "string", "description": "الفرع الهدف"},
            "body": {"type": "string"}},
            "required": ["repo", "title", "head", "base"]}}},
]

_GH_NAMES = {t["function"]["name"] for t in GITHUB_TOOLS}


def is_github_tool(name: str) -> bool:
    return name in _GH_NAMES


def token() -> str:
    return (os.getenv("GITHUB_PAT", "") or os.getenv("GH_PAT", "")).strip().strip('"\'').strip()


def enabled() -> bool:
    return bool(token())


def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "persona-telegram-bot"}


class _GH:
    def __init__(self):
        self._login: str | None = None

    async def _req(self, http, method, path, **kw):
        r = await http.request(method, f"{API_BASE}{path}", headers=_headers(), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise RuntimeError(f"GitHub {r.status_code}: {msg}")
        return r.json() if r.content else {}

    async def login(self, http) -> str:
        if self._login is None:
            self._login = (await self._req(http, "GET", "/user")).get("login", "")
        return self._login

    async def _owner_repo(self, http, repo: str) -> str:
        repo = repo.strip().removeprefix("https://github.com/").strip("/")
        if "/" in repo:
            return repo
        return f"{await self.login(http)}/{repo}"

    async def list_repos(self, http, query=""):
        repos = await self._req(http, "GET", "/user/repos",
                                params={"per_page": 100, "sort": "updated", "affiliation": "owner"})
        names = [r["full_name"] for r in repos]
        if query:
            q = query.lower()
            names = [n for n in names if q in n.lower()]
        return {"repos": names[:100], "count": len(names)}

    async def read_file(self, http, repo, path, ref=None):
        full = await self._owner_repo(http, repo)
        params = {"ref": ref} if ref else {}
        res = await self._req(http, "GET", f"/repos/{full}/contents/{path.lstrip('/')}", params=params)
        if isinstance(res, list):
            return {"error": "هذا مجلد مو ملف؛ استخدم gh_list_dir"}
        if res.get("encoding") == "base64":
            raw = base64.b64decode(res.get("content", "")).decode("utf-8", "replace")
        else:
            raw = res.get("content", "")
        truncated = len(raw) > MAX_READ_CHARS
        return {"repo": full, "path": path, "size": res.get("size"),
                "content": raw[:MAX_READ_CHARS], "truncated": truncated}

    async def list_dir(self, http, repo, path="", ref=None):
        full = await self._owner_repo(http, repo)
        params = {"ref": ref} if ref else {}
        res = await self._req(http, "GET", f"/repos/{full}/contents/{path.strip('/')}", params=params)
        if not isinstance(res, list):
            return {"error": "هذا ملف مو مجلد؛ استخدم gh_read_file"}
        return {"repo": full, "path": path or "/",
                "entries": [{"name": e["name"], "type": e["type"]} for e in res]}

    async def put_file(self, http, repo, path, content, message, branch=None):
        full = await self._owner_repo(http, repo)
        path = path.lstrip("/")
        params = {"ref": branch} if branch else {}
        sha = None
        try:
            cur = await self._req(http, "GET", f"/repos/{full}/contents/{path}", params=params)
            if isinstance(cur, dict):
                sha = cur.get("sha")
        except RuntimeError:
            sha = None
        body = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode()}
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha
        res = await self._req(http, "PUT", f"/repos/{full}/contents/{path}", json=body)
        commit = res.get("commit", {})
        return {"ok": True, "action": "updated" if sha else "created", "repo": full, "path": path,
                "commit": commit.get("sha", "")[:10], "url": res.get("content", {}).get("html_url", "")}

    async def delete_file(self, http, repo, path, message, branch=None):
        full = await self._owner_repo(http, repo)
        path = path.lstrip("/")
        params = {"ref": branch} if branch else {}
        cur = await self._req(http, "GET", f"/repos/{full}/contents/{path}", params=params)
        if not isinstance(cur, dict) or not cur.get("sha"):
            return {"error": "الملف غير موجود"}
        body = {"message": message, "sha": cur["sha"]}
        if branch:
            body["branch"] = branch
        await self._req(http, "DELETE", f"/repos/{full}/contents/{path}", json=body)
        return {"ok": True, "action": "deleted", "repo": full, "path": path}

    async def create_repo(self, http, name, private=True, description="", auto_init=True):
        body = {"name": name, "private": bool(private), "auto_init": bool(auto_init)}
        if description:
            body["description"] = description
        res = await self._req(http, "POST", "/user/repos", json=body)
        return {"ok": True, "repo": res.get("full_name"), "url": res.get("html_url"),
                "default_branch": res.get("default_branch", "main")}

    async def create_branch(self, http, repo, branch, from_branch=None):
        full = await self._owner_repo(http, repo)
        if not from_branch:
            info = await self._req(http, "GET", f"/repos/{full}")
            from_branch = info.get("default_branch", "main")
        ref = await self._req(http, "GET", f"/repos/{full}/git/ref/heads/{from_branch}")
        sha = ref["object"]["sha"]
        await self._req(http, "POST", f"/repos/{full}/git/refs",
                        json={"ref": f"refs/heads/{branch}", "sha": sha})
        return {"ok": True, "repo": full, "branch": branch, "from": from_branch}

    async def open_pr(self, http, repo, title, head, base, body=""):
        full = await self._owner_repo(http, repo)
        res = await self._req(http, "POST", f"/repos/{full}/pulls",
                              json={"title": title, "head": head, "base": base, "body": body})
        return {"ok": True, "number": res.get("number"), "url": res.get("html_url")}


_gh = _GH()
_DISPATCH = {
    "gh_list_repos": lambda h, a: _gh.list_repos(h, a.get("query", "")),
    "gh_read_file": lambda h, a: _gh.read_file(h, a["repo"], a["path"], a.get("ref")),
    "gh_list_dir": lambda h, a: _gh.list_dir(h, a["repo"], a.get("path", ""), a.get("ref")),
    "gh_put_file": lambda h, a: _gh.put_file(h, a["repo"], a["path"], a["content"], a["message"], a.get("branch")),
    "gh_delete_file": lambda h, a: _gh.delete_file(h, a["repo"], a["path"], a["message"], a.get("branch")),
    "gh_create_repo": lambda h, a: _gh.create_repo(h, a["name"], a.get("private", True),
                                                   a.get("description", ""), a.get("auto_init", True)),
    "gh_create_branch": lambda h, a: _gh.create_branch(h, a["repo"], a["branch"], a.get("from_branch")),
    "gh_open_pr": lambda h, a: _gh.open_pr(h, a["repo"], a["title"], a["head"], a["base"], a.get("body", "")),
}


async def run_github(name: str, raw_args: str) -> str:
    if not enabled():
        return "GitHub غير مربوط: أضف السر GITHUB_PAT عشان أقدر أكتب وأعدّل على GitHub."
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return "وسائط غير صالحة."
    fn = _DISPATCH.get(name)
    if not fn:
        return f"أداة غير معروفة: {name}"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            return json.dumps(await fn(http, args), ensure_ascii=False)
    except KeyError as e:
        return f"ناقص وسيط مطلوب: {e}"
    except Exception as e:  # نرجع الخطأ للموديل بدل ما يطيح البوت
        return f"فشلت أداة GitHub: {e}"


def describe_github(name: str, raw_args: str) -> str:
    try:
        a = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        a = {}
    repo = a.get("repo", "") or a.get("name", "")
    return {
        "gh_list_repos": "🐙 أعرض المستودعات",
        "gh_read_file": f"📄 أقرأ {repo}:{a.get('path','')}",
        "gh_list_dir": f"📂 محتويات {repo}/{a.get('path','')}",
        "gh_put_file": f"✍️ أكتب {repo}:{a.get('path','')}",
        "gh_delete_file": f"🗑 أحذف {repo}:{a.get('path','')}",
        "gh_create_repo": f"➕ مستودع جديد {a.get('name','')}",
        "gh_create_branch": f"🌿 فرع {a.get('branch','')} في {repo}",
        "gh_open_pr": f"🔀 Pull Request في {repo}",
    }.get(name, f"🐙 {name}")

"""أدوات GitHub للبوت — للمالك فقط. تحكّم كامل تقريباً بمستودعات المالك:

- الملفات والفروع والمستودعات والـ PR (الأدوات الأساسية gh_*).
- gh_repo: معلومات، فروع، commits، مقارنة، بحث في الكود، إصدارات.
- gh_pr: عرض/ملفات/تعليقات/دمج/تحديث/إغلاق الـ PR.
- gh_issue: عرض وإنشاء وتعديل والتعليق على الـ issues.
- gh_actions: الـ workflows والتشغيلات، سجلات الأخطاء، إعادة التشغيل، الإلغاء، التشغيل اليدوي،
  و«watch» يتابع CI مباشرة لين يخلص ويرجّع سبب الفشل.

التوكن من السر GH_PAT. ما فيه حذف مستودعات (إجراء خطير وغير قابل للتراجع).
البوت يتأكد إن الطالب هو المالك قبل ما يشغّل أي أداة هنا (الفحص في bot.py).
"""

import asyncio
import base64
import json
import os
import re
import time

import httpx

API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
MAX_READ_CHARS = 50_000
WATCH_POLL = float(os.getenv("GH_WATCH_POLL", "15"))   # كل كم ثانية نفحص CI وقت المتابعة

_REPO = {"type": "string", "description": "owner/name أو name"}

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
            "repo": _REPO, "path": {"type": "string"},
            "ref": {"type": "string", "description": "فرع أو commit (اختياري)"}},
            "required": ["repo", "path"]}}},
    {"type": "function", "function": {
        "name": "gh_list_dir",
        "description": "اعرض محتويات مجلد في مستودع (أو الجذر لو path فاضي).",
        "parameters": {"type": "object", "properties": {
            "repo": _REPO, "path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["repo"]}}},
    {"type": "function", "function": {
        "name": "gh_put_file",
        "description": "أنشئ ملفاً جديداً أو عدّل ملفاً موجوداً مباشرة على GitHub (يكتب المحتوى كامل ويعمل commit).",
        "parameters": {"type": "object", "properties": {
            "repo": _REPO, "path": {"type": "string"},
            "content": {"type": "string", "description": "محتوى الملف كامل"},
            "message": {"type": "string", "description": "رسالة الـ commit"},
            "branch": {"type": "string", "description": "الفرع (اختياري، الافتراضي الفرع الرئيسي)"}},
            "required": ["repo", "path", "content", "message"]}}},
    {"type": "function", "function": {
        "name": "gh_delete_file",
        "description": "احذف ملفاً من مستودع (يعمل commit).",
        "parameters": {"type": "object", "properties": {
            "repo": _REPO, "path": {"type": "string"},
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
            "repo": _REPO, "branch": {"type": "string"},
            "from_branch": {"type": "string", "description": "الفرع المصدر (اختياري)"}},
            "required": ["repo", "branch"]}}},
    {"type": "function", "function": {
        "name": "gh_open_pr",
        "description": "افتح Pull Request.",
        "parameters": {"type": "object", "properties": {
            "repo": _REPO, "title": {"type": "string"},
            "head": {"type": "string", "description": "الفرع اللي فيه التغييرات"},
            "base": {"type": "string", "description": "الفرع الهدف"},
            "body": {"type": "string"}},
            "required": ["repo", "title", "head", "base"]}}},
    {"type": "function", "function": {
        "name": "gh_repo",
        "description": ("معلومات وتاريخ المستودع. action: info (الوصف والفرع الرئيسي وآخر commit) · branches · "
                        "commits (sha/branch/path/limit) · commit (sha: الملفات والفرق) · compare (base, head) · "
                        "search_code (query) · release (tag, title, body, branch) · delete_branch (branch)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["info", "branches", "commits", "commit", "compare",
                                                  "search_code", "release", "delete_branch"]},
            "repo": _REPO, "sha": {"type": "string"}, "branch": {"type": "string"},
            "path": {"type": "string"}, "base": {"type": "string"}, "head": {"type": "string"},
            "query": {"type": "string"}, "tag": {"type": "string"}, "title": {"type": "string"},
            "body": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["action", "repo"]}}},
    {"type": "function", "function": {
        "name": "gh_pr",
        "description": ("Pull Requests. action: list (state) · get (number: التفاصيل وقابلية الدمج وحالة CI) · "
                        "files (number: الملفات والفرق) · comments (number) · comment (number, body) · "
                        "merge (number, method: merge|squash|rebase) · update_branch (number) · close (number)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "files", "comments", "comment", "merge",
                                                  "update_branch", "close"]},
            "repo": _REPO, "number": {"type": "integer"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "body": {"type": "string"}, "method": {"type": "string", "enum": ["merge", "squash", "rebase"]}},
            "required": ["action", "repo"]}}},
    {"type": "function", "function": {
        "name": "gh_issue",
        "description": ("Issues. action: list (state, labels) · get (number مع التعليقات) · create (title, body, labels) · "
                        "update (number, title/body/state/labels) · comment (number, body)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "create", "update", "comment"]},
            "repo": _REPO, "number": {"type": "integer"}, "title": {"type": "string"},
            "body": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "labels": {"type": "array", "items": {"type": "string"}}},
            "required": ["action", "repo"]}}},
    {"type": "function", "function": {
        "name": "gh_actions",
        "description": ("GitHub Actions / CI. action: workflows · runs (branch, status, workflow, limit) · "
                        "run (run_id: الوظائف والخطوات) · logs (run_id أو job_id: سجل الأخطاء المختصر) · "
                        "watch (run_id أو branch/sha: يتابع CI مباشرة لين يخلص ويرجّع سبب أي فشل) · "
                        "rerun (run_id, failed_only) · cancel (run_id) · dispatch (workflow, branch, inputs)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["workflows", "runs", "run", "logs", "watch", "rerun",
                                                  "cancel", "dispatch"]},
            "repo": _REPO, "run_id": {"type": "integer"}, "job_id": {"type": "integer"},
            "branch": {"type": "string"}, "sha": {"type": "string"},
            "status": {"type": "string", "description": "مثل failure, success, in_progress, queued"},
            "workflow": {"type": "string", "description": "اسم ملف الـ workflow (ci.yml) أو رقمه"},
            "inputs": {"type": "object"}, "failed_only": {"type": "boolean"},
            "timeout": {"type": "integer", "description": "مهلة watch بالثواني (الافتراضي 900)"},
            "limit": {"type": "integer"}},
            "required": ["action", "repo"]}}},
]

_GH_NAMES = {t["function"]["name"] for t in GITHUB_TOOLS}


def is_github_tool(name: str) -> bool:
    return name in _GH_NAMES


def token() -> str:
    return (os.getenv("GH_PAT", "") or os.getenv("GITHUB_PAT", "")).strip().strip('"\'').strip()


def enabled() -> bool:
    return bool(token())


def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "persona-telegram-bot"}


def _short(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"…[+{len(text) - n} حرف]"


_TS = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ERR = re.compile(r"##\[error\]|\b(error|Error|ERROR|FAIL|FAILED|Failed|failed|Traceback|Exception|panic|fatal)\b")


def log_excerpt(text: str, limit: int = 7000) -> str:
    """من سجل GitHub Actions: سطور الأخطاء + آخر السجل (بدون الطوابع الزمنية والضجيج)."""
    lines = [_TS.sub("", ln).rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip() and not ln.startswith(("##[group]", "##[endgroup]"))]
    errors = [ln.replace("##[error]", "❌ ") for ln in lines if _ERR.search(ln)][-40:]
    tail = lines[-120:]
    out = ""
    if errors:
        out += "سطور الأخطاء:\n" + "\n".join(errors) + "\n\n"
    out += "آخر السجل:\n" + "\n".join(tail)
    if len(out) > limit:
        out = out[: limit // 3] + "\n…\n" + out[-(limit - limit // 3):]
    return out


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

    async def _text(self, http, path) -> str:
        """للسجلات: GitHub يحوّل لرابط نصي مؤقت (httpx ما يرسل التوكن لمضيف ثاني)."""
        r = await http.get(f"{API_BASE}{path}", headers=_headers(), follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {r.status_code}: {r.text[:200]}")
        return r.text

    async def login(self, http) -> str:
        if self._login is None:
            self._login = (await self._req(http, "GET", "/user")).get("login", "")
        return self._login

    async def _owner_repo(self, http, repo: str) -> str:
        repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if "/" in repo:
            return repo
        return f"{await self.login(http)}/{repo}"

    # ----- الأساسية -----

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

    # ----- gh_repo -----

    async def repo(self, http, a):
        full = await self._owner_repo(http, a["repo"])
        act = a["action"]
        limit = max(1, min(int(a.get("limit") or 20), 100))
        if act == "info":
            r = await self._req(http, "GET", f"/repos/{full}")
            out = {k: r.get(k) for k in ("full_name", "description", "private", "default_branch", "language",
                                         "open_issues_count", "pushed_at", "html_url")}
            try:
                c = await self._req(http, "GET", f"/repos/{full}/commits", params={"per_page": 1})
                if c:
                    out["last_commit"] = {"sha": c[0]["sha"][:10],
                                          "message": (c[0].get("commit", {}).get("message") or "").split("\n")[0]}
            except RuntimeError:
                pass
            return out
        if act == "branches":
            res = await self._req(http, "GET", f"/repos/{full}/branches", params={"per_page": 100})
            return {"branches": [{"name": b["name"], "protected": b.get("protected", False)} for b in res]}
        if act == "commits":
            params = {"per_page": limit}
            if a.get("branch") or a.get("sha"):
                params["sha"] = a.get("branch") or a.get("sha")
            if a.get("path"):
                params["path"] = a["path"]
            res = await self._req(http, "GET", f"/repos/{full}/commits", params=params)
            return {"commits": [{"sha": c["sha"][:10], "message": (c.get("commit", {}).get("message") or "").split("\n")[0][:120],
                                 "author": (c.get("commit", {}).get("author") or {}).get("name"),
                                 "date": (c.get("commit", {}).get("author") or {}).get("date")} for c in res]}
        if act == "commit":
            c = await self._req(http, "GET", f"/repos/{full}/commits/{a['sha']}")
            budget, files = 14_000, []
            for f in c.get("files") or []:
                patch = _short(f.get("patch"), 1500)
                budget -= len(patch)
                files.append({"file": f.get("filename"), "status": f.get("status"), "+": f.get("additions"),
                              "-": f.get("deletions"), **({"patch": patch} if budget > 0 else {})})
            return {"sha": c.get("sha", "")[:10], "message": (c.get("commit") or {}).get("message"),
                    "stats": c.get("stats"), "files": files[:60]}
        if act == "compare":
            c = await self._req(http, "GET", f"/repos/{full}/compare/{a['base']}...{a['head']}")
            return {"status": c.get("status"), "ahead_by": c.get("ahead_by"), "behind_by": c.get("behind_by"),
                    "commits": [{"sha": x["sha"][:10], "message": (x.get("commit", {}).get("message") or "").split("\n")[0][:100]}
                                for x in (c.get("commits") or [])[-30:]],
                    "files": [{"file": f.get("filename"), "status": f.get("status"), "+": f.get("additions"),
                               "-": f.get("deletions")} for f in (c.get("files") or [])[:80]]}
        if act == "search_code":
            res = await self._req(http, "GET", "/search/code",
                                  params={"q": f"{a['query']} repo:{full}", "per_page": min(limit, 50)})
            return {"total": res.get("total_count"), "items": [i.get("path") for i in res.get("items") or []]}
        if act == "release":
            body = {"tag_name": a["tag"], "name": a.get("title") or a["tag"], "body": a.get("body", "")}
            if a.get("branch"):
                body["target_commitish"] = a["branch"]
            r = await self._req(http, "POST", f"/repos/{full}/releases", json=body)
            return {"ok": True, "url": r.get("html_url"), "tag": r.get("tag_name")}
        if act == "delete_branch":
            info = await self._req(http, "GET", f"/repos/{full}")
            if a["branch"] == info.get("default_branch"):
                return {"error": "ما أحذف الفرع الرئيسي للمستودع"}
            await self._req(http, "DELETE", f"/repos/{full}/git/refs/heads/{a['branch']}")
            return {"ok": True, "deleted_branch": a["branch"]}
        return {"error": f"action غير معروف: {act}"}

    # ----- gh_pr -----

    async def _checks(self, http, full, ref) -> dict:
        """ملخص حالة CI لـ commit/فرع: check runs + statuses."""
        out = {}
        try:
            cr = await self._req(http, "GET", f"/repos/{full}/commits/{ref}/check-runs", params={"per_page": 50})
            runs = cr.get("check_runs") or []
            out["checks"] = [{"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion")}
                             for c in runs[:30]]
        except RuntimeError:
            pass
        try:
            st = await self._req(http, "GET", f"/repos/{full}/commits/{ref}/status")
            if st.get("statuses"):
                out["combined_status"] = st.get("state")
        except RuntimeError:
            pass
        return out

    async def pr(self, http, a):
        full = await self._owner_repo(http, a["repo"])
        act, n = a["action"], a.get("number")
        if act == "list":
            res = await self._req(http, "GET", f"/repos/{full}/pulls",
                                  params={"state": a.get("state") or "open", "per_page": 30})
            return {"pulls": [{"number": p["number"], "title": p["title"], "head": p["head"]["ref"],
                               "base": p["base"]["ref"], "draft": p.get("draft"), "user": p["user"]["login"],
                               "updated": p.get("updated_at")} for p in res]}
        if n is None:
            return {"error": "number مطلوب"}
        if act == "get":
            p = await self._req(http, "GET", f"/repos/{full}/pulls/{n}")
            out = {"number": n, "title": p.get("title"), "state": p.get("state"), "merged": p.get("merged"),
                   "draft": p.get("draft"), "head": p["head"]["ref"], "head_sha": p["head"]["sha"][:10],
                   "base": p["base"]["ref"], "mergeable": p.get("mergeable"),
                   "mergeable_state": p.get("mergeable_state"), "changed_files": p.get("changed_files"),
                   "additions": p.get("additions"), "deletions": p.get("deletions"),
                   "body": _short(p.get("body"), 3000), "url": p.get("html_url")}
            out.update(await self._checks(http, full, p["head"]["sha"]))
            return out
        if act == "files":
            res = await self._req(http, "GET", f"/repos/{full}/pulls/{n}/files", params={"per_page": 100})
            budget, files = 16_000, []
            for f in res:
                patch = _short(f.get("patch"), 2000)
                budget -= len(patch)
                files.append({"file": f["filename"], "status": f["status"], "+": f["additions"], "-": f["deletions"],
                              **({"patch": patch} if budget > 0 else {})})
            return {"files": files}
        if act == "comments":
            issue = await self._req(http, "GET", f"/repos/{full}/issues/{n}/comments", params={"per_page": 50})
            review = await self._req(http, "GET", f"/repos/{full}/pulls/{n}/comments", params={"per_page": 50})
            return {"comments": [{"user": c["user"]["login"], "body": _short(c.get("body"), 800)} for c in issue],
                    "review_comments": [{"user": c["user"]["login"], "path": c.get("path"), "line": c.get("line"),
                                         "body": _short(c.get("body"), 800)} for c in review]}
        if act == "comment":
            r = await self._req(http, "POST", f"/repos/{full}/issues/{n}/comments", json={"body": a["body"]})
            return {"ok": True, "url": r.get("html_url")}
        if act == "merge":
            r = await self._req(http, "PUT", f"/repos/{full}/pulls/{n}/merge",
                                json={"merge_method": a.get("method") or "squash"})
            return {"ok": bool(r.get("merged", True)), "sha": (r.get("sha") or "")[:10], "message": r.get("message")}
        if act == "update_branch":
            r = await self._req(http, "PUT", f"/repos/{full}/pulls/{n}/update-branch", json={})
            return {"ok": True, "message": r.get("message")}
        if act == "close":
            await self._req(http, "PATCH", f"/repos/{full}/pulls/{n}", json={"state": "closed"})
            return {"ok": True, "closed": n}
        return {"error": f"action غير معروف: {act}"}

    # ----- gh_issue -----

    async def issue(self, http, a):
        full = await self._owner_repo(http, a["repo"])
        act, n = a["action"], a.get("number")
        if act == "list":
            params = {"state": a.get("state") or "open", "per_page": 30}
            if a.get("labels"):
                params["labels"] = ",".join(a["labels"])
            res = await self._req(http, "GET", f"/repos/{full}/issues", params=params)
            return {"issues": [{"number": i["number"], "title": i["title"], "state": i["state"],
                                "labels": [lb["name"] for lb in i.get("labels") or []], "user": i["user"]["login"]}
                               for i in res if "pull_request" not in i]}
        if act == "create":
            body = {"title": a["title"], "body": a.get("body", "")}
            if a.get("labels"):
                body["labels"] = a["labels"]
            r = await self._req(http, "POST", f"/repos/{full}/issues", json=body)
            return {"ok": True, "number": r.get("number"), "url": r.get("html_url")}
        if n is None:
            return {"error": "number مطلوب"}
        if act == "get":
            i = await self._req(http, "GET", f"/repos/{full}/issues/{n}")
            cs = await self._req(http, "GET", f"/repos/{full}/issues/{n}/comments", params={"per_page": 30})
            return {"number": n, "title": i.get("title"), "state": i.get("state"), "body": _short(i.get("body"), 4000),
                    "labels": [lb["name"] for lb in i.get("labels") or []],
                    "comments": [{"user": c["user"]["login"], "body": _short(c.get("body"), 800)} for c in cs]}
        if act == "update":
            body = {k: a[k] for k in ("title", "body", "state", "labels") if a.get(k) is not None}
            if body.get("state") == "all":
                body.pop("state")
            r = await self._req(http, "PATCH", f"/repos/{full}/issues/{n}", json=body)
            return {"ok": True, "state": r.get("state"), "url": r.get("html_url")}
        if act == "comment":
            r = await self._req(http, "POST", f"/repos/{full}/issues/{n}/comments", json={"body": a["body"]})
            return {"ok": True, "url": r.get("html_url")}
        return {"error": f"action غير معروف: {act}"}

    # ----- gh_actions -----

    @staticmethod
    def _run_row(r: dict) -> dict:
        return {"id": r["id"], "workflow": r.get("name"), "branch": r.get("head_branch"),
                "sha": (r.get("head_sha") or "")[:7], "event": r.get("event"), "status": r.get("status"),
                "conclusion": r.get("conclusion"), "created": r.get("created_at"), "url": r.get("html_url")}

    async def runs(self, http, full, branch=None, status=None, workflow=None, limit=10, sha=None) -> list[dict]:
        params = {"per_page": max(1, min(int(limit or 10), 50))}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        if sha:
            params["head_sha"] = sha
        path = f"/repos/{full}/actions/workflows/{workflow}/runs" if workflow else f"/repos/{full}/actions/runs"
        res = await self._req(http, "GET", path, params=params)
        return res.get("workflow_runs") or []

    async def jobs(self, http, full, run_id) -> list[dict]:
        res = await self._req(http, "GET", f"/repos/{full}/actions/runs/{run_id}/jobs", params={"per_page": 50})
        return res.get("jobs") or []

    async def failure_digest(self, http, full, run_id, limit: int = 9000) -> dict:
        """سبب فشل تشغيل: الوظائف الفاشلة وخطواتها ومقتطف من سجلها."""
        jobs = await self.jobs(http, full, run_id)
        failed = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure")]
        out = []
        per = max(1500, limit // max(1, len(failed[:3])))
        for j in failed[:3]:
            steps = [s.get("name") for s in j.get("steps") or [] if s.get("conclusion") == "failure"]
            try:
                log = log_excerpt(await self._text(http, f"/repos/{full}/actions/jobs/{j['id']}/logs"), per)
            except Exception as e:  # السجل ممكن يكون انحذف أو ما عندنا صلاحية
                log = f"(ما قدرت أجيب السجل: {e})"
            out.append({"job": j.get("name"), "job_id": j["id"], "conclusion": j.get("conclusion"),
                        "failed_steps": steps, "log": log})
        return {"failed_jobs": out, "jobs_total": len(jobs)}

    async def actions(self, http, a, progress=None):
        full = await self._owner_repo(http, a["repo"])
        act = a["action"]
        if act == "workflows":
            res = await self._req(http, "GET", f"/repos/{full}/actions/workflows")
            return {"workflows": [{"id": w["id"], "name": w["name"], "file": (w.get("path") or "").split("/")[-1],
                                   "state": w.get("state")} for w in res.get("workflows") or []]}
        if act == "runs":
            rs = await self.runs(http, full, a.get("branch"), a.get("status"), a.get("workflow"), a.get("limit") or 10,
                                 a.get("sha"))
            return {"runs": [self._run_row(r) for r in rs]}
        if act == "run":
            r = await self._req(http, "GET", f"/repos/{full}/actions/runs/{a['run_id']}")
            jobs = await self.jobs(http, full, a["run_id"])
            return {**self._run_row(r), "jobs": [
                {"id": j["id"], "name": j.get("name"), "status": j.get("status"), "conclusion": j.get("conclusion"),
                 "failed_steps": [s.get("name") for s in j.get("steps") or [] if s.get("conclusion") == "failure"]}
                for j in jobs]}
        if act == "logs":
            if a.get("job_id"):
                return {"job_id": a["job_id"],
                        "log": log_excerpt(await self._text(http, f"/repos/{full}/actions/jobs/{a['job_id']}/logs"))}
            return await self.failure_digest(http, full, a["run_id"])
        if act == "rerun":
            path = "rerun-failed-jobs" if a.get("failed_only", True) else "rerun"
            await self._req(http, "POST", f"/repos/{full}/actions/runs/{a['run_id']}/{path}", json={})
            return {"ok": True, "rerun": a["run_id"], "failed_only": a.get("failed_only", True)}
        if act == "cancel":
            await self._req(http, "POST", f"/repos/{full}/actions/runs/{a['run_id']}/cancel", json={})
            return {"ok": True, "cancelled": a["run_id"]}
        if act == "dispatch":
            ref = a.get("branch") or (await self._req(http, "GET", f"/repos/{full}")).get("default_branch", "main")
            await self._req(http, "POST", f"/repos/{full}/actions/workflows/{a['workflow']}/dispatches",
                            json={"ref": ref, "inputs": a.get("inputs") or {}})
            return {"ok": True, "workflow": a["workflow"], "ref": ref,
                    "note": "انطلق — تابعه بـ watch (branch) بعد ثواني"}
        if act == "watch":
            return await self.watch(http, full, a, progress)
        return {"error": f"action غير معروف: {act}"}

    async def watch(self, http, full, a, progress=None):
        """يتابع CI لين يخلص: تشغيل محدد، أو كل التشغيلات لآخر commit على فرع/sha."""
        timeout = max(30, min(int(a.get("timeout") or 900), 1800))
        deadline = time.monotonic() + timeout
        sha = a.get("sha")
        if not a.get("run_id") and not sha:
            branch = a.get("branch") or (await self._req(http, "GET", f"/repos/{full}")).get("default_branch", "main")
            sha = (await self._req(http, "GET", f"/repos/{full}/commits/{branch}")).get("sha")
            if not sha:
                return {"error": f"ما لقيت آخر commit على {branch}"}
        first_seen = time.monotonic()
        while True:
            if a.get("run_id"):
                runs = [await self._req(http, "GET", f"/repos/{full}/actions/runs/{a['run_id']}")]
            else:
                runs = [r for r in await self.runs(http, full, sha=sha, limit=30) if (r.get("head_sha") or "").startswith(sha)]
            pending = [r for r in runs if r.get("status") != "completed"]
            if progress:
                if runs:
                    done = len(runs) - len(pending)
                    names = "، ".join(f"{r.get('name')}: {r.get('status') if r.get('status') != 'completed' else r.get('conclusion')}"
                                     for r in runs[:4])
                    progress(f"👀 يتابع CI ({done}/{len(runs)} خلصت) — {names}")
                else:
                    progress("👀 ينتظر يبدأ CI…")
            if runs and not pending:
                break
            if not runs and time.monotonic() - first_seen > min(90, timeout):
                return {"status": "no_runs", "sha": (sha or "")[:10],
                        "note": "ما اشتغل أي workflow على هذا الـ commit (يمكن المستودع ما فيه CI لهذا الفرع)."}
            if time.monotonic() > deadline:
                return {"status": "timeout", "runs": [self._run_row(r) for r in runs],
                        "note": "CI ما خلص خلال المهلة — تابعه لاحقاً بـ watch أو runs"}
            await asyncio.sleep(WATCH_POLL)
        result = {"status": "completed", "sha": (sha or runs[0].get("head_sha") or "")[:10],
                  "runs": [self._run_row(r) for r in runs]}
        bad = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
        result["all_green"] = not bad
        if bad:
            result["failures"] = []
            for r in bad[:3]:
                result["failures"].append({"run_id": r["id"], "workflow": r.get("name"),
                                           **(await self.failure_digest(http, full, r["id"], 8000))})
        return result


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
    "gh_repo": lambda h, a: _gh.repo(h, a),
    "gh_pr": lambda h, a: _gh.pr(h, a),
    "gh_issue": lambda h, a: _gh.issue(h, a),
}


async def run_github(name: str, raw_args: str, progress=None) -> str:
    if not enabled():
        return "GitHub غير مربوط: أضف السر GH_PAT عشان أقدر أكتب وأعدّل على GitHub."
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return "وسائط غير صالحة."
    if name == "gh_actions":
        async def fn(h, a):
            return await _gh.actions(h, a, progress)
    else:
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


async def login() -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        return await _gh.login(http)


async def recent_runs(repo: str, branch: str | None = None, limit: int = 10) -> list[dict]:
    """للمتابعة بالخلفية: آخر تشغيلات CI لمستودع."""
    async with httpx.AsyncClient(timeout=30) as http:
        full = await _gh._owner_repo(http, repo)
        return [_GH._run_row(r) | {"head_sha": r.get("head_sha")} for r in await _gh.runs(http, full, branch, limit=limit)]


async def failure_digest(repo: str, run_id: int) -> dict:
    async with httpx.AsyncClient(timeout=60) as http:
        full = await _gh._owner_repo(http, repo)
        return await _gh.failure_digest(http, full, run_id, 6000)


async def rerun(repo: str, run_id: int) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        full = await _gh._owner_repo(http, repo)
        await _gh._req(http, "POST", f"/repos/{full}/actions/runs/{run_id}/rerun-failed-jobs", json={})


def describe_github(name: str, raw_args: str) -> str:
    try:
        a = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        a = {}
    repo = a.get("repo", "") or a.get("name", "")
    act = a.get("action", "")
    if name == "gh_actions":
        return {"watch": f"👀 يتابع CI في {repo}", "logs": f"🧾 يقرأ سجل أخطاء CI في {repo}",
                "rerun": f"🔁 يعيد تشغيل CI في {repo}", "cancel": f"⏹ يلغي تشغيل CI في {repo}",
                "dispatch": f"🚀 يشغّل workflow {a.get('workflow', '')} في {repo}",
                "run": f"⚙️ تفاصيل تشغيل CI في {repo}"}.get(act, f"⚙️ Actions ({act}) في {repo}")
    if name == "gh_pr":
        return {"merge": f"🔀 يدمج PR #{a.get('number', '')} في {repo}",
                "comment": f"💬 يعلّق على PR #{a.get('number', '')}"}.get(act, f"🔀 PR ({act}) في {repo}")
    if name == "gh_issue":
        return f"🐞 Issue ({act}) في {repo}"
    if name == "gh_repo":
        return f"🐙 {repo}: {act}"
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

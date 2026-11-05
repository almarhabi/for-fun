"""أدوات البحث في الإنترنت اللي يقدر البوت يستخدمها."""

import asyncio
import json

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "ابحث في الإنترنت عن معلومات حديثة أو أي شيء ما تعرفه أكيد. يرجع عناوين وروابط ومقتطفات.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "نص البحث"},
                    "max_results": {"type": "integer", "description": "عدد النتائج (1-10)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "افتح صفحة ويب واقرأ نصها، لما تحتاج تفاصيل أكثر من المقتطف.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "رابط الصفحة"}},
                "required": ["url"],
            },
        },
    },
]


def _search(query: str, max_results: int = 5) -> list[dict]:
    max_results = max(1, min(int(max_results or 5), 10))
    results = DDGS().text(query, max_results=max_results)
    return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in results]


async def _open_url(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as http:
        r = await http.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PersonaBot/1.0)"})
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return text[:8000]


async def run_tool(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}")
        if name == "web_search":
            out = await asyncio.to_thread(_search, args["query"], args.get("max_results", 5))
        elif name == "open_url":
            out = await _open_url(args["url"])
        else:
            out = f"أداة غير معروفة: {name}"
    except Exception as e:  # نرجع الخطأ للموديل بدل ما يطيح البوت
        out = f"فشلت الأداة: {e}"
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def describe_call(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    if name == "web_search":
        return f"🔎 بحث: {args.get('query', '')}"
    if name == "open_url":
        return f"🌐 فتح: {args.get('url', '')}"
    return f"🛠 {name}"

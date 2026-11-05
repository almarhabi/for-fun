"""أدوات البحث في الإنترنت — أقوى: يبحث، ويجيب محتوى الصفحات الأعلى فعلياً، ويقرأ أي رابط بعمق.

الفرق عن قبل: web_search ما يرجّع مقتطفات صغيرة فقط، بل يفتح أفضل النتائج بالتوازي ويستخرج
النص الحقيقي منها ويعطيه للموديل مباشرة — فيجاوب بمعلومات فعلية ومصادر، مو بس عناوين.
"""

import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

import config

log = logging.getLogger("persona-bot.tools")

# وكيل اختياري لكل طلبات الويب (Tor مثلاً). فاضي = اتصال مباشر.
WEB_PROXY = config.WEB_PROXY or None
if WEB_PROXY:
    log.info("طلبات الويب عبر وكيل: %s", WEB_PROXY)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "ar,en;q=0.9"}
RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}
PER_PAGE_CHARS = 2800      # كم حرف نقتطع من كل صفحة داخل البحث
OPEN_URL_CHARS = 15000     # قراءة رابط مباشرة: أعمق
FETCH_TOP = 4              # كم نتيجة نفتح محتواها تلقائياً داخل البحث
SEARCH_BUDGET = 14000      # سقف إجمالي لحجم نتيجة البحث (حروف) عشان ما ينفخ السياق

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("ابحث في الإنترنت. يرجّع نتائج مع عناوين وروابط ومقتطفات، "
                        "ويفتح أفضل النتائج ويستخرج نصها الحقيقي حتى تجاوب بمعلومات مؤكدة ومصادر. "
                        "استخدمه لأي معلومة حديثة أو ما تعرفها أكيد، وللأسئلة اللي تحتاج أرقام أو تفاصيل."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "نص البحث. كن محدداً."},
            "max_results": {"type": "integer", "description": "عدد النتائج 1-12 (الافتراضي 6)"},
            "recency": {"type": "string", "enum": ["day", "week", "month", "year"],
                        "description": "فلترة بالحداثة (اختياري) — للأخبار والأحداث الجديدة"},
            "fetch": {"type": "boolean", "description": "يفتح محتوى أفضل النتائج (الافتراضي true). خله false لو تبي مقتطفات بس"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "افتح صفحة ويب واقرأ نصها الكامل بعمق (أكثر من المقتطف). استخدمه لما تبي تفاصيل صفحة محددة.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "رابط الصفحة"}},
            "required": ["url"]}}},
]

_DROP = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form",
         "button", "svg", "iframe", "figure", "figcaption"]


def _search(query: str, max_results: int, recency: str | None) -> list[dict]:
    max_results = max(1, min(int(max_results or 6), 12))
    kw = {"region": "wt-wt", "safesearch": "off", "max_results": max_results}
    if recency in RECENCY:
        kw["timelimit"] = RECENCY[recency]
    ddgs = DDGS(proxy=WEB_PROXY) if WEB_PROXY else DDGS()  # نفس الوكيل للبحث
    results = ddgs.text(query, **kw)
    return [{"title": (r.get("title") or "").strip(),
             "url": r.get("href") or r.get("url") or "",
             "snippet": (r.get("body") or "").strip()}
            for r in results if (r.get("href") or r.get("url"))]


def _extract(html_text: str, base_url: str = "") -> tuple[str, str]:
    """يرجّع (العنوان، النص النظيف) — يفضّل المحتوى الأساسي (article/main) ويشيل الزوائد."""
    soup = BeautifulSoup(html_text, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    for tag in soup(_DROP):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
    if main is None:  # أكبر كتلة نص (<div>/<section>) كتقريب للمحتوى الأساسي
        blocks = soup.find_all(["div", "section"])
        main = max(blocks, key=lambda b: len(b.get_text(" ", strip=True)), default=None) or soup.body or soup
    parts = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "li", "p", "pre", "blockquote", "td", "th"]):
        txt = el.get_text(" ", strip=True)
        if txt and len(txt) > 1:
            prefix = "# " if el.name in ("h1", "h2", "h3", "h4") else ("- " if el.name == "li" else "")
            parts.append(prefix + txt)
    text = "\n".join(parts) or main.get_text(" ", strip=True)
    text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return title, text


async def _fetch(url: str, client: httpx.AsyncClient, cap: int) -> dict:
    try:
        r = await client.get(url, headers=UA, timeout=15, follow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400:
            return {"url": url, "error": f"HTTP {r.status_code}"}
        if "html" not in ctype and "text" not in ctype:
            return {"url": url, "error": f"نوع غير مدعوم ({ctype or '؟'})"}
        title, text = _extract(r.text, url)
        truncated = len(text) > cap
        return {"url": url, "title": title, "content": text[:cap], "truncated": truncated}
    except Exception as e:
        return {"url": url, "error": str(e)[:160]}


async def _fetch_many(urls: list[str], cap: int) -> dict[str, dict]:
    kw = {"headers": UA, "follow_redirects": True}
    if WEB_PROXY:  # فتح الصفحات عبر نفس الوكيل (يدعم socks5h للـ Tor و .onion)
        kw["proxy"] = WEB_PROXY
    async with httpx.AsyncClient(**kw) as client:
        results = await asyncio.gather(*(_fetch(u, client, cap) for u in urls))
    return {r["url"]: r for r in results}


async def _web_search(query: str, max_results: int, recency: str | None, fetch: bool) -> tuple[dict, list[dict]]:
    results = await asyncio.to_thread(_search, query, max_results, recency)
    if not results:
        return {"query": query, "results": [], "note": "ما فيه نتائج — جرّب صياغة ثانية"}, []
    sources = [{"title": r["title"], "url": r["url"]} for r in results]
    if fetch:
        pages = await _fetch_many([r["url"] for r in results[:FETCH_TOP]], PER_PAGE_CHARS)
        budget = SEARCH_BUDGET
        for r in results:
            page = pages.get(r["url"])
            if page and page.get("content") and budget > 0:
                chunk = page["content"][:budget]
                r["page_content"] = chunk
                budget -= len(chunk)
    return {"query": query, "recency": recency, "results": results}, sources


async def _open_url(url: str) -> tuple[dict, list[dict]]:
    pages = await _fetch_many([url], OPEN_URL_CHARS)
    page = pages[url]
    if page.get("error"):
        return {"url": url, "error": page["error"]}, []
    return page, [{"title": page.get("title", ""), "url": url}]


async def run_tool(name: str, raw_args: str) -> tuple[str, list[dict]]:
    """يشغّل الأداة ويرجّع (النتيجة للموديل، المصادر للعرض)."""
    try:
        args = json.loads(raw_args or "{}")
        if name == "web_search":
            out, sources = await _web_search(args["query"], args.get("max_results", 6),
                                             args.get("recency"), args.get("fetch", True))
        elif name == "open_url":
            out, sources = await _open_url(args["url"])
        else:
            return f"أداة غير معروفة: {name}", []
    except KeyError as e:
        return f"ناقص وسيط مطلوب: {e}", []
    except Exception as e:  # نرجّع الخطأ للموديل بدل ما يطيح البوت
        log.warning("tool %s failed: %s", name, e)
        return f"فشلت الأداة: {e}", []
    return json.dumps(out, ensure_ascii=False), sources


def describe_call(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    if name == "web_search":
        rec = {"day": " (آخر يوم)", "week": " (آخر أسبوع)", "month": " (آخر شهر)", "year": " (آخر سنة)"}
        return f"🔎 بحث: {args.get('query', '')}" + rec.get(args.get("recency"), "")
    if name == "open_url":
        host = urlparse(args.get("url", "")).netloc.removeprefix("www.")
        return f"📖 قراءة: {host or args.get('url', '')}"
    return f"🛠 {name}"

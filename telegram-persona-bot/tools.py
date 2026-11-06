"""Web search tools — stronger: searches, actually fetches the content of the top pages, and reads any link in depth.

web_search doesn't return only small snippets: it opens the best results in parallel, extracts their real text
and hands it to the model directly — so answers are based on actual information and sources, not just titles.
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

# Optional proxy for all web requests (Tor, for example). Empty = direct connection.
WEB_PROXY = config.WEB_PROXY or None
if WEB_PROXY:
    log.info("Web requests go through a proxy: %s", WEB_PROXY)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "ar,en;q=0.9"}
RECENCY = {"day": "d", "week": "w", "month": "m", "year": "y"}
PER_PAGE_CHARS = 2800      # characters taken from each page during a search
OPEN_URL_CHARS = 15000     # reading a link directly: deeper
FETCH_TOP = 4              # how many results get their content fetched automatically during a search
SEARCH_BUDGET = 14000      # overall cap on a search result's size (characters) so it doesn't bloat the context

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("Search the web. Returns results with titles, links and snippets, and opens the best results "
                        "and extracts their real text so you can answer with verified information and sources. "
                        "Use it for anything recent or anything you don't know for sure, and for questions that need "
                        "numbers or details."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query. Be specific."},
            "max_results": {"type": "integer", "description": "Number of results 1-12 (default 6)"},
            "recency": {"type": "string", "enum": ["day", "week", "month", "year"],
                        "description": "Filter by recency (optional) — for news and recent events"},
            "fetch": {"type": "boolean", "description": "Open the content of the best results (default true). Set false for snippets only"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Open a web page and read its full text in depth (more than the snippet). Use it when you need a specific page's details.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The page URL"}},
            "required": ["url"]}}},
]

_DROP = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form",
         "button", "svg", "iframe", "figure", "figcaption"]


def _search(query: str, max_results: int, recency: str | None) -> list[dict]:
    max_results = max(1, min(int(max_results or 6), 12))
    kw = {"region": "wt-wt", "safesearch": "off", "max_results": max_results}
    if recency in RECENCY:
        kw["timelimit"] = RECENCY[recency]
    ddgs = DDGS(proxy=WEB_PROXY) if WEB_PROXY else DDGS()  # the same proxy for searching
    results = ddgs.text(query, **kw)
    return [{"title": (r.get("title") or "").strip(),
             "url": r.get("href") or r.get("url") or "",
             "snippet": (r.get("body") or "").strip()}
            for r in results if (r.get("href") or r.get("url"))]


def _extract(html_text: str, base_url: str = "") -> tuple[str, str]:
    """Returns (title, clean text) — prefers the main content (article/main) and drops the clutter."""
    soup = BeautifulSoup(html_text, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    for tag in soup(_DROP):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
    if main is None:  # the biggest text block (<div>/<section>) approximates the main content
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
            return {"url": url, "error": f"unsupported content type ({ctype or '?'})"}
        title, text = _extract(r.text, url)
        truncated = len(text) > cap
        return {"url": url, "title": title, "content": text[:cap], "truncated": truncated}
    except Exception as e:
        return {"url": url, "error": str(e)[:160]}


async def _fetch_many(urls: list[str], cap: int) -> dict[str, dict]:
    kw = {"headers": UA, "follow_redirects": True}
    if WEB_PROXY:  # open pages through the same proxy (supports socks5h for Tor and .onion)
        kw["proxy"] = WEB_PROXY
    async with httpx.AsyncClient(**kw) as client:
        results = await asyncio.gather(*(_fetch(u, client, cap) for u in urls))
    return {r["url"]: r for r in results}


async def _web_search(query: str, max_results: int, recency: str | None, fetch: bool) -> tuple[dict, list[dict]]:
    results = await asyncio.to_thread(_search, query, max_results, recency)
    if not results:
        return {"query": query, "results": [], "note": "No results — try different wording"}, []
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
    """Runs the tool and returns (the result for the model, the sources to display)."""
    try:
        args = json.loads(raw_args or "{}")
        if name == "web_search":
            out, sources = await _web_search(args["query"], args.get("max_results", 6),
                                             args.get("recency"), args.get("fetch", True))
        elif name == "open_url":
            out, sources = await _open_url(args["url"])
        else:
            return f"Unknown tool: {name}", []
    except KeyError as e:
        return f"Missing required argument: {e}", []
    except Exception as e:  # return the error to the model instead of crashing the bot
        log.warning("tool %s failed: %s", name, e)
        return f"Tool failed: {e}", []
    return json.dumps(out, ensure_ascii=False), sources


def describe_call(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    if name == "web_search":
        rec = {"day": " (past day)", "week": " (past week)", "month": " (past month)", "year": " (past year)"}
        return f"🔎 Search: {args.get('query', '')}" + rec.get(args.get("recency"), "")
    if name == "open_url":
        host = urlparse(args.get("url", "")).netloc.removeprefix("www.")
        return f"📖 Reading: {host or args.get('url', '')}"
    return f"🛠 {name}"

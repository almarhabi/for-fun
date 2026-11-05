"""تحويل ردود الموديل (Markdown) إلى رسائل تليجرام مرتبة (HTML) مع ضبط اتجاه النص.

- ** و ## و - ... تتحول لخط عريض وعناوين وقوائم حقيقية بدل ما تطلع كرموز.
- كل سطر عربي يبدأ بعلامة اتجاه (RLM) عشان يطلع من اليمين لليسار حتى لو بدأ بكلمة إنجليزية أو رقم.
- علامات الاقتباس المزخرفة “” ‘’ تتحول لـ «» في النص العربي، ورموز مثل ^^ تنحذف.
"""

import html
import re
from urllib.parse import urlparse

RLM, LRM = "‏", "‎"
CURSOR = "▌"
PAGE_LIMIT = 3000  # حروف Markdown لكل رسالة (حد تليجرام 4096 بعد التحويل)
TG_LIMIT = 4096

_AR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LAT = re.compile(r"[A-Za-z]")
_TAG = re.compile(r"<(/?)([a-z-]+)(?:\s[^>]*)?>")
_ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.\-]*)")
_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_UL = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_OL = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MATH = re.compile(r"\$([^$\n]*[\\^_][^$\n]*)\$")
_INLINE_RULES = [
    (re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"), r"<b><i>\1</i></b>"),
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"<b>\1</b>"),
    (re.compile(r"==(?=\S)(.+?)(?<=\S)=="), r"<b>\1</b>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"<s>\1</s>"),
    (re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|"), r"<tg-spoiler>\1</tg-spoiler>"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"), r"<i>\1</i>"),
]
_SUP = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def to_plain(html_text: str) -> str:
    """نسخة بدون تنسيق (لو تليجرام رفض الـ HTML لأي سبب)."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def valid_html(s: str) -> bool:
    stack = []
    for m in _TAG.finditer(s):
        closing, name = m.group(1), m.group(2)
        if name not in _ALLOWED:
            return False
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


# ---------- اتجاه النص ----------

def _counts(text: str) -> tuple[int, int]:
    return len(_AR.findall(text)), len(_LAT.findall(text))


def is_rtl(text: str) -> bool:
    """هل الرسالة عربية في الغالب؟ (بدون حساب الأكواد)"""
    text = re.sub(r"```.*?(```|$)", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    ar, lat = _counts(text)
    return ar > 0 and ar * 2 >= lat


def mark(line_html: str, plain: str, rtl: bool) -> str:
    """يضيف علامة الاتجاه المناسبة لبداية السطر."""
    if not plain.strip():
        return line_html
    ar, lat = _counts(plain)
    if rtl or (ar and ar >= lat):
        out = RLM + line_html
        # سطر إنجليزي داخل رد عربي: نثبت علامة الترقيم في آخره بجانب الكلمة
        if ar * 2 < lat and re.search(r"[A-Za-z][^A-Za-z؀-ۿ]*$", plain) and re.search(r"[^\w\s]\s*$", plain):
            out += LRM
        return out
    return line_html


# ---------- التنسيق داخل السطر ----------

def _clean_symbols(s: str, arabic_line: bool) -> str:
    s = re.sub(r"(?<!\S)\^[_\-.o]?\^(?!\S)", "", s)  # ^^ و ^_^
    s = re.sub(r"(?<=[\w)\]])\^\{?([0-9+\-]{1,4})\}?", lambda m: m.group(1).translate(_SUP), s)
    s = s.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "").replace("$$", "")
    if arabic_line:
        def quote(m):
            inner = m.group(1)
            return f"«{inner}»" if _AR.search(inner) else m.group(0)
        s = re.sub(r"[“\"]([^“”\"\n]+?)[”\"]", quote, s)
        s = re.sub(r"(?<!\w)[‘']([^‘’'\n]+?)[’'](?!\w)", quote, s)
        s = s.replace("“", "«").replace("”", "»")
    return re.sub(r"(?<=\S)  +(?=\S)", " ", s)


def inline(text: str, partial: bool = False) -> str:
    """يحول تنسيق سطر واحد (عريض، مائل، كود، روابط...) إلى HTML آمن."""
    if partial:
        # أثناء الكتابة: نقفل التنسيق المفتوح عشان يطلع عريض من أول حرف بدل **
        if text.count("**") % 2:
            text += "**"
        if len(re.findall(r"(?<!`)`(?!`)", text)) % 2:
            text += "`"
    arabic_line = bool(_AR.search(text))
    holders: list[str] = []

    def hold(s: str) -> str:
        holders.append(s)
        return f"{len(holders) - 1}"

    def fmt(seg: str) -> str:
        seg = _MATH.sub(lambda m: hold(f"<code>{esc(m.group(1))}</code>"), seg)
        seg = _LINK.sub(
            lambda m: hold(f'<a href="{html.escape(m.group(2), quote=True)}">{fmt(m.group(1))}</a>'), seg
        )
        seg = esc(_clean_symbols(seg, arabic_line))
        for pat, rep in _INLINE_RULES:
            seg = pat.sub(rep, seg)
        return seg.replace("**", "")  # أي ** باقية بدون زوج ما لها داعي تطلع

    out, pos = [], 0
    for m in _CODE_SPAN.finditer(text):
        out.append(fmt(text[pos : m.start()]))
        out.append(hold(f"<code>{esc(m.group(2).strip())}</code>"))
        pos = m.end()
    out.append(fmt(text[pos:]))
    result = "".join(out)
    for _ in range(3):  # الروابط ممكن تكون داخل بعض
        result = re.sub(r"(\d+)", lambda m: holders[int(m.group(1))], result)
    if not valid_html(result):
        return esc(text.replace("**", ""))
    return result


# ---------- الجداول ----------

def _cells(row: str) -> list[str]:
    r = row.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", r)]


def _table(rows: list[str], rtl: bool) -> str:
    """الجداول ما تنعرض زين على الجوال، فنحولها لقائمة مرتبة."""
    header, body = _cells(rows[0]), [_cells(r) for r in rows[1:] if r.strip()]
    if not body:
        joined = " · ".join(header)
        return mark(f"<b>{inline(joined)}</b>", joined, rtl)
    if len(header) <= 2:
        lines = []
        for r in body:
            first, second = r[0] if r else "", r[1] if len(r) > 1 else ""
            line = f"• <b>{inline(first)}</b>" + (f": {inline(second)}" if second else "")
            lines.append(mark(line, first + " " + second, rtl))
        return "\n".join(lines)
    blocks = []
    for r in body:
        title = r[0] if r else ""
        block = [mark(f"🔹 <b>{inline(title)}</b>", title, rtl)]
        for h, c in zip(header[1:], r[1:]):
            if c:
                block.append(mark(f"   ◦ <i>{inline(h)}</i>: {inline(c)}", h + " " + c, rtl))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


# ---------- الرسالة كاملة ----------

def render(md: str, partial: bool = False) -> str:
    """Markdown ← HTML تليجرام. partial=True لما يكون الرد لسا ينكتب."""
    md = strip_split(md.replace("\r\n", "\n"), partial).strip("\n")
    rtl = is_rtl(md)
    lines = md.split("\n")
    n = len(lines)
    out: list[str] = []
    i = 0
    while i < n:
        line = lines[i]
        last = partial and i == n - 1

        fence = _FENCE.match(line)
        if fence:
            marker, lang = fence.group(1)[0] * 3, fence.group(2)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # سطر الإغلاق
            code = "\n".join(body).strip("\n")
            if code:
                cls = f' class="language-{esc(lang)}"' if lang else ""
                out.append(f"<pre><code{cls}>{esc(code)}</code></pre>")
            continue

        if "|" in line and i + 1 < n and _TSEP.match(lines[i + 1]):
            rows = [line]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            if out and out[-1] != "":
                out.append("")
            out.append(_table(rows, rtl))
            out.append("")
            continue

        head = _HEAD.match(line)
        if head:
            if out and out[-1] != "":
                out.append("")  # مسافة قبل كل عنوان عشان تبان الأقسام
            content = inline(head.group(2), last)
            html_line = f"<b><u>{content}</u></b>" if len(head.group(1)) == 1 else f"<b>{content}</b>"
            out.append(mark(html_line, head.group(2), rtl))
            i += 1
            continue

        if _HR.match(line):
            out.append("━━━━━━━━━━")
            i += 1
            continue

        if _QUOTE.match(line):
            quoted = []
            while i < n and (q := _QUOTE.match(lines[i])):
                quoted.append(q.group(1))
                i += 1
            inner = "\n".join(mark(inline(x, partial and i >= n), x, rtl) for x in quoted if x.strip())
            if inner:
                out.append(f"<blockquote>{inner}</blockquote>")
            continue

        ul = _UL.match(line)
        if ul:
            level = min(len(ul.group(1).expandtabs(4)) // 2, 3)
            text = ul.group(2)
            task = _TASK.match(text)
            if task:
                bullet, text = ("✅" if task.group(1).lower() == "x" else "⬜"), task.group(2)
            else:
                bullet = "•◦▪▫"[level]
            out.append(mark("   " * level + bullet + " " + inline(text, last), text, rtl))
            i += 1
            continue

        ol = _OL.match(line)
        if ol:
            level = min(len(ol.group(1).expandtabs(4)) // 2, 3)
            out.append(mark("   " * level + f"<b>{ol.group(2)}.</b> " + inline(ol.group(3), last), ol.group(3), rtl))
            i += 1
            continue

        out.append(mark(inline(line.rstrip(), last).rstrip(), line, rtl) if line.strip() else "")
        i += 1

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result).strip("\n")
    return result


def _open_fence(text: str) -> tuple[str, str] | None:
    """لو النص ينتهي داخل بلوك كود، يرجع (العلامة، اللغة)."""
    state = None
    for line in text.split("\n"):
        f = _FENCE.match(line)
        if state is None and f:
            state = (f.group(1)[0] * 3, f.group(2))
        elif state is not None and line.strip().startswith(state[0]):
            state = None
    return state


SPLIT_MARK = "<<<split>>>"  # علامة قديمة؛ ما عاد نطلبها من الموديل، بس ننظّفها لو ظهرت
_SPLIT_RE = re.compile(r"[ \t]*<<<split>>>[ \t]*")
_SPLIT_PARTIAL = re.compile(r"<{1,3}(?:s(?:p(?:l(?:i(?:t(?:>{0,2})?)?)?)?)?)?\Z")


def strip_split(md: str, partial: bool = False) -> str:
    """يشيل علامة <<<split>>> من النص (وأي بداية علامة وهو لسا يكتبها)."""
    md = _SPLIT_RE.sub("\n\n", md)
    if partial:
        md = _SPLIT_PARTIAL.sub("", md)
    return md


def _best_cut(window: str, limit: int) -> int:
    """أفضل مكان للقطع داخل النافذة: قبل عنوان، ثم نهاية فقرة، ثم سطر، ثم نهاية جملة، ثم مسافة."""
    heads = [m.start() for m in re.finditer(r"\n(?=[ \t]{0,3}#{1,6}\s)", window)]
    if heads and heads[-1] >= limit * 0.4:
        return heads[-1]
    cut = window.rfind("\n\n")
    if cut >= limit * 0.5:
        return cut
    cut = window.rfind("\n")
    if cut >= limit * 0.4:
        return cut
    ends = [m.end() for m in re.finditer(r"[.!?؟…؛:](?=\s)", window)]
    if ends and ends[-1] >= limit * 0.3:
        return ends[-1]
    cut = window.rfind(" ")
    return cut if cut > 0 else limit


def split_md(text: str, limit: int = PAGE_LIMIT) -> list[str]:
    """يقسم الرد لعدة رسائل: أولاً عند علامات الموديل <<<split>>>، وبعدها لو جزء طال عن الحد
    يقطع عند أقرب نقطة طبيعية. التقسيم ثابت: الأجزاء المرسلة ما تتغير لما يكمل الرد."""
    for k in range(len(SPLIT_MARK) - 1, 2, -1):  # علامة لسا ما اكتملت وهو يكتب
        if text.endswith(SPLIT_MARK[:k]):
            text = text[:-k]
            break
    segments = _SPLIT_RE.split(text)
    chunks: list[str] = []
    carry = ""
    for n, seg in enumerate(segments):
        rest = carry + seg.strip("\n")
        carry = ""
        while len(rest) > limit:
            cut = _best_cut(rest[:limit], limit)
            piece, rest = rest[:cut], rest[cut:].lstrip("\n")
            fence = _open_fence(piece)
            if fence:  # نقفل بلوك الكود هنا ونفتحه من جديد في الرسالة اللي بعدها
                piece += "\n" + fence[0]
                rest = fence[0] + fence[1] + "\n" + rest
            if piece.strip():
                chunks.append(piece)
        fence = _open_fence(rest)
        if fence and n < len(segments) - 1:
            rest += "\n" + fence[0]
            carry = fence[0] + fence[1] + "\n"
        if rest.strip():
            chunks.append(rest)
    return chunks or [""]


def page_html(md: str, partial: bool) -> str:
    h = render(md, partial) if md.strip() else ""
    if len(h) > TG_LIMIT - 200:
        h = esc(md)[: TG_LIMIT - 200]
    return h


# ---------- التفكير والمصادر ----------

def duration_ar(seconds: float) -> str:
    s = int(round(seconds))
    if s < 1:
        return "أقل من ثانية"

    def unit(n: int, one: str, two: str, few: str, many: str) -> str:
        if n == 1:
            return one
        if n == 2:
            return two
        return f"{n} {few}" if 3 <= n <= 10 else f"{n} {many}"

    if s < 60:
        return unit(s, "ثانية", "ثانيتين", "ثوانٍ", "ثانية")
    m, rem = divmod(s, 60)
    text = unit(m, "دقيقة", "دقيقتين", "دقائق", "دقيقة")
    return text + (f" و{unit(rem, 'ثانية', 'ثانيتين', 'ثوانٍ', 'ثانية')}" if rem else "")


def thinking_html(parts: list[list[str]], final: bool, seconds: float, limit: int = 3000) -> tuple[str, bool]:
    """رسالة التفكير. parts = [["r", نص تفكير] أو ["s", خطوة بحث]]. يرجع (html، هل انقص)."""
    items: list[tuple[str, str]] = []
    for kind, text in parts:
        if kind == "s":
            items.append(("s", text))
        else:
            for line in text.strip().split("\n"):
                line = re.sub(r"^\s*#+\s*", "", line).rstrip()
                if line.strip():
                    items.append(("r", line))
    truncated = False
    total = 0
    kept: list[tuple[str, str]] = []
    for kind, text in reversed(items):  # نعرض آخر جزء لو التفكير طويل
        if total + len(text) > limit:
            room = limit - total
            if room > 80 and kind == "r":
                kept.append((kind, "…" + text[-room:]))
            truncated = True
            break
        kept.append((kind, text))
        total += len(text) + 1
    kept.reverse()
    rtl = is_rtl("\n".join(t for _, t in kept))
    lines = []
    for kind, text in kept:
        if kind == "s":
            lines.append(mark(f"<b>{esc(text)}</b>", text, rtl))
        else:
            bullet = _UL.match(text)
            body = ("• " + inline(bullet.group(2))) if bullet else inline(text)
            lines.append(mark(body, text, rtl))
    title = f"💭 <b>فكّر لمدة {duration_ar(seconds)}</b>" if final else "💭 <b>يفكر…</b>"
    title = RLM + title
    if not lines:
        return title, False
    if truncated:
        lines.insert(0, RLM + "<i>…(جزء من التفكير)</i>")
    tag = "blockquote expandable" if final else "blockquote"
    return f"{title}\n<{tag}>" + "\n".join(lines) + "</blockquote>", truncated


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def sources_html(sources: list[dict], limit: int = 6) -> str:
    items = []
    for n, s in enumerate(sources[:limit], 1):
        title = (s.get("title") or domain(s["url"]) or s["url"]).strip()
        if len(title) > 70:
            title = title[:67] + "…"
        link = f'<a href="{html.escape(s["url"], quote=True)}">{esc(title)}</a>'
        items.append(mark(f"{n}. {link} <i>· {esc(domain(s['url']))}</i>", title, True))
    return f"<blockquote expandable>{RLM}🔗 <b>المصادر</b>\n" + "\n".join(items) + "</blockquote>"


# ================= تحويل لصفحة Telegra.ph (للردود الطويلة) =================
# Telegra.ph ينفتح داخل تليجرام بـ Instant View: صفحة مرتبة بلا حد حروف وبلا تقسيم.
# الوسوم المسموحة في Telegra.ph (نحوّل # و ## لـ h3/h4، والجداول لقوائم).

from html.parser import HTMLParser as _HTMLParser

_INLINE_TAG = {"b": "b", "strong": "b", "i": "i", "em": "i", "u": "u", "s": "s",
               "code": "code", "a": "a", "tg-spoiler": "i", "br": "br"}


class _NodeParser(_HTMLParser):
    """يحوّل HTML تليجرام المبسّط (من inline()) إلى عُقد Telegra.ph."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: list = []
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        t = _INLINE_TAG.get(tag)
        if not t:
            return
        if t == "br":
            self.stack[-1].append({"tag": "br"})
            return
        node = {"tag": t, "children": []}
        if t == "a":
            node["attrs"] = {"href": dict(attrs).get("href", "")}
        self.stack[-1].append(node)
        self.stack.append(node["children"])

    def handle_startendtag(self, tag, attrs):
        if _INLINE_TAG.get(tag) == "br":
            self.stack[-1].append({"tag": "br"})

    def handle_endtag(self, tag):
        t = _INLINE_TAG.get(tag)
        if t and t != "br" and len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if data:
            self.stack[-1].append(data)


def _inline_nodes(text: str, rtl: bool) -> list:
    nodes = _NodeParser()
    p = nodes
    p.feed(inline(text))
    p.close()
    children = p.root
    if rtl and _AR.search(text):
        children = [RLM, *children]  # نجبر اتجاه الفقرة لليمين
    return children or [""]


def _is_block_start(line: str) -> bool:
    return bool(_FENCE.match(line) or _HEAD.match(line) or _HR.match(line)
               or _QUOTE.match(line) or _UL.match(line) or _OL.match(line))


def _table_nodes(rows: list[str], rtl: bool) -> list:
    header, body = _cells(rows[0]), [_cells(r) for r in rows[1:] if r.strip()]
    out = []
    for r in body:
        title = r[0] if r else ""
        out.append({"tag": "p", "children": _inline_nodes("**" + title + "**", rtl)})
        pairs = [f"{h}: {c}" for h, c in zip(header[1:], r[1:]) if c]
        if pairs:
            out.append({"tag": "ul", "children": [{"tag": "li", "children": _inline_nodes(p, rtl)} for p in pairs]})
    return out or [{"tag": "p", "children": _inline_nodes(" · ".join(header), rtl)}]


def to_telegraph_nodes(md: str) -> list:
    """Markdown ← عُقد Telegra.ph."""
    md = strip_split(md.replace("\r\n", "\n")).strip("\n")
    rtl = is_rtl(md)
    lines = md.split("\n")
    n = len(lines)
    nodes: list = []
    i = 0
    while i < n:
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0] * 3
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body).strip("\n")
            if code:
                nodes.append({"tag": "pre", "children": [code]})
            continue
        if "|" in line and i + 1 < n and _TSEP.match(lines[i + 1]):
            rows = [line]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            nodes.extend(_table_nodes(rows, rtl))
            continue
        head = _HEAD.match(line)
        if head:
            tag = "h3" if len(head.group(1)) <= 2 else "h4"
            nodes.append({"tag": tag, "children": _inline_nodes(head.group(2), rtl)})
            i += 1
            continue
        if _HR.match(line):
            nodes.append({"tag": "hr"})
            i += 1
            continue
        if _QUOTE.match(line):
            quoted = []
            while i < n and (q := _QUOTE.match(lines[i])):
                quoted.append(q.group(1))
                i += 1
            kids: list = []
            for x in quoted:
                if x.strip():
                    if kids:
                        kids.append({"tag": "br"})
                    kids.extend(_inline_nodes(x, rtl))
            if kids:
                nodes.append({"tag": "blockquote", "children": kids})
            continue
        ul, ol = _UL.match(line), _OL.match(line)
        if ul or ol:
            ordered = bool(ol)
            items = []
            while i < n:
                m = _OL.match(lines[i]) if ordered else _UL.match(lines[i])
                if not m:
                    break
                txt = m.group(3) if ordered else m.group(2)
                task = _TASK.match(txt)
                if task:
                    txt = ("✅ " if task.group(1).lower() == "x" else "⬜ ") + task.group(2)
                items.append({"tag": "li", "children": _inline_nodes(txt, rtl)})
                i += 1
            nodes.append({"tag": "ol" if ordered else "ul", "children": items})
            continue
        if line.strip():
            para = [line]
            i += 1
            while i < n and lines[i].strip() and not _is_block_start(lines[i]):
                para.append(lines[i])
                i += 1
            kids = []
            for j, pl in enumerate(para):
                if j:
                    kids.append({"tag": "br"})
                kids.extend(_inline_nodes(pl, rtl))
            nodes.append({"tag": "p", "children": kids})
            continue
        i += 1
    return nodes or [{"tag": "p", "children": [""]}]


def telegraph_title(md: str, limit: int = 120) -> str:
    """عنوان الصفحة: أول سطر فيه نص، بدون رموز Markdown."""
    for line in strip_split(md).splitlines():
        t = line.strip()
        t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t)
        t = re.sub(r"^\s{0,3}>\s?", "", t)
        t = re.sub(r"^\s*([-*+•]|\d{1,3}[.)])\s+", "", t)
        t = re.sub(r"[*_`~#]", "", t).replace(RLM, "").replace(LRM, "").strip()
        if t:
            return t[:limit].rstrip() + ("…" if len(t) > limit else "")
    return "رد"


def live_tail_html(md: str, limit: int = 3400) -> str:
    """معاينة حيّة للرد وهو يُكتب: النص كامل لو قصير، وإلا آخر جزء منه بصيغة نصية آمنة."""
    md = strip_split(md.replace("\r\n", "\n"), partial=True).strip("\n")
    if not md:
        return ""
    full = render(md, partial=True)
    if len(full) <= TG_LIMIT - 80:
        return full
    plain = to_plain(render(md, partial=True))
    tail = plain[-limit:]
    lines = ("…" + tail).split("\n")
    return "\n".join(RLM + esc(ln) if ln.strip() else "" for ln in lines)

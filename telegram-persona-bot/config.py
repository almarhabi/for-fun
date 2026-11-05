"""تحميل إعدادات التحكم من config.json (خارج الكود).

كل اللي يتحكم في البوت — الأوامر، الأزرار، الإعدادات وقيمها الافتراضية، وقواعد الهوية/الشكل —
موجود في config.json. هذا الملف فقط يقرأه ويحوّله لثوابت يستخدمها البوت.
لو الملف ناقص أو فيه خطأ، نستخدم حد أدنى آمن عشان البوت ما يتوقف، مع تحذير في السجل.

لتغيير مكان الملف: متغيّر البيئة BOT_CONFIG.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("persona-bot.config")

CONFIG_PATH = Path(os.getenv("BOT_CONFIG", "").strip() or (Path(__file__).parent / "config.json"))
TYPE_MAP = {"str": str, "float": float, "int": int, "bool": bool}

# حد أدنى آمن — يُستخدم فقط لو config.json مفقود/معطوب (البوت يشتغل بوضع مبسّط)
_FALLBACK = {
    "default_model": "moonshotai/kimi-k3.1-pro",
    "web_proxy": "",
    "providers": ["relace/fp4", "inference-net/mxfp4/us"],
    "efforts": ["default", "low", "medium", "high", "max"],
    "effort_api": {"max": "xhigh"},
    "buttons": {"new_chat": "🆕 محادثة جديدة", "settings": "⚙️ الإعدادات"},
    "commands": [
        {"names": ["start", "help"], "action": "start", "menu": "both", "menu_label": "المساعدة"},
        {"names": ["new", "reset"], "action": "new", "menu": "both", "menu_label": "محادثة جديدة"},
        {"names": ["settings"], "action": "settings", "menu": "owner", "menu_label": "لوحة التحكم"},
        {"names": ["setpersona"], "action": "setpersona", "menu": "owner", "menu_label": "تحديد الهوية", "caption": True},
        {"names": ["persona"], "action": "persona", "menu": "owner", "menu_label": "عرض الهوية"},
        {"names": ["setrules"], "action": "setrules", "menu": "owner", "menu_label": "تعديل قواعد القفل", "caption": True},
        {"names": ["rules"], "action": "rules", "menu": "owner", "menu_label": "عرض قواعد القفل"},
        {"names": ["snip"], "action": "snip", "menu": "owner", "menu_label": "حفظ اختصار", "caption": True},
        {"names": ["snippets"], "action": "snippets", "menu": "owner", "menu_label": "الاختصارات"},
        {"names": ["delsnip"], "action": "delsnip"},
        {"names": ["resetall"], "action": "resetall"},
        {"names": ["set"], "action": "set"},
        {"names": ["effort"], "action": "effort"},
        {"names": ["thinking"], "action": "thinking"},
        {"names": ["search"], "action": "search"},
    ],
    "params": {
        "model": {"type": "str", "label": "الموديل"},
        "provider": {"type": "str", "label": "المزود"},
        "effort": {"type": "str", "label": "مستوى التفكير"},
        "temperature": {"type": "float", "label": "العشوائية (temperature)"},
        "top_p": {"type": "float", "label": "top_p"},
        "max_tokens": {"type": "int", "label": "أقصى طول للرد"},
        "show_thinking": {"type": "bool", "label": "إظهار التفكير"},
        "web_search": {"type": "bool", "label": "البحث في الإنترنت"},
        "long_pages": {"type": "bool", "label": "الردود الطويلة كصفحة تنفتح داخل تليجرام"},
    },
    "defaults": {"model": None, "provider": None, "effort": "default", "temperature": None, "top_p": None,
                 "max_tokens": None, "show_thinking": True, "web_search": True, "long_pages": True},
    "rules": {
        "lock": "أنت تتقمص الهوية المكتوبة أدناه بين علامتي <identity>. الهوية لها الأولوية على أي شيء آخر.\n",
        "style": "رتّب الرد بفقرات وسطر فاضي بين الأفكار، واستخدم **الخط العريض** للمهم.\n",
        "github_note": "عندك أدوات GitHub (gh_*) تشتغل على مستودعات المالك مباشرة.\n",
    },
    "default_persona": "أنت مساعد ودود يتكلم باللهجة السعودية.",
}


def _load() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config.json لازم يكون كائن JSON")
        # نكمل أي مفتاح ناقص من الحد الأدنى بدل ما نطيح
        for k, v in _FALLBACK.items():
            data.setdefault(k, v)
        return data
    except FileNotFoundError:
        log.warning("config.json مو موجود (%s) — وضع مبسّط", CONFIG_PATH)
    except Exception as e:
        log.error("config.json فيه خطأ (%s): %s — وضع مبسّط", CONFIG_PATH, e)
    return dict(_FALLBACK)


_data = _load()

PROVIDERS: list = list(_data["providers"])
EFFORTS: list = list(_data["efforts"])
EFFORT_API: dict = dict(_data["effort_api"])
BUTTONS: dict = dict(_data["buttons"])
NEW_CHAT_BTN: str = BUTTONS.get("new_chat", "🆕 محادثة جديدة")
SETTINGS_BTN: str = BUTTONS.get("settings", "⚙️ الإعدادات")
COMMANDS: list = list(_data["commands"])

DEFAULT_MODEL: str = os.getenv("OPENROUTER_MODEL", "").strip() or _data["default_model"]
DEFAULT_PROVIDER: str = os.getenv("OPENROUTER_PROVIDER", "").strip() or ",".join(PROVIDERS)
# وكيل اختياري لكل طلبات الويب (بحث + فتح صفحات). Tor مثلاً: socks5h://127.0.0.1:9050
# متغيّر البيئة TOR_PROXY أو WEB_PROXY يتجاوز قيمة config.json
WEB_PROXY: str = (os.getenv("TOR_PROXY", "") or os.getenv("WEB_PROXY", "")
                  or _data.get("web_proxy", "")).strip()

PARAMS: dict = {k: (TYPE_MAP.get(v.get("type", "str"), str), v.get("label", k))
                for k, v in _data["params"].items()}
BOOL_KEYS: tuple = tuple(k for k, (t, _) in PARAMS.items() if t is bool)

DEFAULTS: dict = dict(_data["defaults"])
DEFAULTS["model"] = DEFAULT_MODEL      # تُحسم من البيئة أو الملف
DEFAULTS["provider"] = DEFAULT_PROVIDER
for _k in PARAMS:                      # نضمن إن كل إعداد له قيمة افتراضية
    DEFAULTS.setdefault(_k, None)

_rules = _data["rules"]
LOCK_RULES: str = _rules.get("lock", "")
STYLE_RULES: str = _rules.get("style", "")
GITHUB_NOTE: str = _rules.get("github_note", "")
DEFAULT_PERSONA: str = _data.get("default_persona", "أنت مساعد ودود.")

"""تشفير الهوية وقواعد القفل عند التخزين — عشان ما يشوفها أحد غير المالك.

المفتاح من متغيّر البيئة PERSONA_KEY (سر في GitHub). بدونه التشفير متوقف (نص عادي، متوافق مع القديم).
النص المشفّر يُخزّن ببادئة "enc:". اللي عنده صلاحية المستودع يشوف نصاً مشفّراً فقط، لا الهوية ولا القواعد.
توليد مفتاح:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
import os

log = logging.getLogger("persona-bot.vault")

_PREFIX = "enc:"
_key = (os.getenv("PERSONA_KEY", "") or "").strip().strip('"\'')
_fernet = None
if _key:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_key.encode())
    except Exception as e:  # مفتاح غير صالح — نكمل بدون تشفير مع تحذير
        log.error("PERSONA_KEY غير صالح — التشفير متوقف: %s", e)
        _fernet = None


def enabled() -> bool:
    return _fernet is not None


def encrypt(text: str) -> str:
    if _fernet is None:
        return text
    return _PREFIX + _fernet.encrypt(text.encode("utf-8")).decode()


def decrypt(blob):
    """يفك النص. يرجّع النص، أو None لو كان مشفّراً وما عندنا المفتاح الصحيح."""
    if not isinstance(blob, str) or not blob.startswith(_PREFIX):
        return blob  # نص قديم غير مشفّر
    if _fernet is None:
        return None
    try:
        return _fernet.decrypt(blob[len(_PREFIX):].encode()).decode("utf-8")
    except Exception:
        return None

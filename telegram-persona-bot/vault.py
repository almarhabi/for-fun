"""Encrypts the persona and lock rules at rest — so nobody but the owner can read them.

The key comes from the PERSONA_KEY environment variable (a GitHub secret). Without it encryption is off (plain
text, compatible with older data). Encrypted text is stored with an "enc:" prefix. Anyone with access to the
repository sees only ciphertext — neither the persona nor the rules.
Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
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
    except Exception as e:  # invalid key — continue without encryption, with a warning
        log.error("PERSONA_KEY is invalid — encryption is off: %s", e)
        _fernet = None


def enabled() -> bool:
    return _fernet is not None


def encrypt(text: str) -> str:
    if _fernet is None:
        return text
    return _PREFIX + _fernet.encrypt(text.encode("utf-8")).decode()


def decrypt(blob):
    """Decrypts the text. Returns it, or None if it's encrypted and we don't have the right key."""
    if not isinstance(blob, str) or not blob.startswith(_PREFIX):
        return blob  # older, unencrypted text
    if _fernet is None:
        return None
    try:
        return _fernet.decrypt(blob[len(_PREFIX):].encode()).decode("utf-8")
    except Exception:
        return None

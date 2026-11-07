"""Encrypts the persona and lock rules at rest — so nobody but the owner can read them.

The key comes from the PERSONA_KEY environment variable (a GitHub secret). Without it encryption is off (plain
text, compatible with older data). Encrypted text is stored with an "enc:" prefix. Anyone with access to the
repository sees only ciphertext — neither the persona nor the rules.
Any passphrase works (a non-Fernet value is turned into a key with SHA-256); a generated one is best:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
import os

log = logging.getLogger("persona-bot.vault")

_PREFIX = "enc:"
_key = (os.getenv("PERSONA_KEY", "") or "").strip().strip('"\'')
_fernet = None
if _key:
    from cryptography.fernet import Fernet
    try:
        _fernet = Fernet(_key.encode())
    except Exception:
        # Not a Fernet key (any passphrase works): derive a proper one from it with SHA-256,
        # so encryption is never silently off just because of the key's format.
        import base64
        import hashlib
        _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_key.encode("utf-8")).digest()))
        log.info("PERSONA_KEY isn't a Fernet key — using a key derived from it (encryption is on)")


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

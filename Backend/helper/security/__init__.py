"""
security/ — crypto, password hashing, and userbot session auth helpers.

  - encrypt        encode_string / decode_string (compressed base62)
  - passwords      hash_password / verify_password
  - session_auth   Telegram userbot login / session lifecycle (lazy)

FastAPI-layer auth (require_auth, verify_token) stays under
Backend.fastapi.security.

session_auth is loaded lazily so importing encrypt/passwords during
Backend package bootstrap does not pull pyrogram / Backend.db.
"""

from Backend.helper.security.encrypt import decode_string, encode_string
from Backend.helper.security.passwords import hash_password, is_hashed, verify_password

__all__ = [
    "decode_string",
    "encode_string",
    "hash_password",
    "is_hashed",
    "verify_password",
    "disconnect_session",
    "get_active_session_string",
    "get_session_status",
    "reconnect_session",
    "remove_session",
    "start_login",
    "submit_code",
    "submit_password",
]

_LAZY = {
    "disconnect_session": ("Backend.helper.security.session_auth", "disconnect_session"),
    "get_active_session_string": ("Backend.helper.security.session_auth", "get_active_session_string"),
    "get_session_status": ("Backend.helper.security.session_auth", "get_session_status"),
    "reconnect_session": ("Backend.helper.security.session_auth", "reconnect_session"),
    "remove_session": ("Backend.helper.security.session_auth", "remove_session"),
    "start_login": ("Backend.helper.security.session_auth", "start_login"),
    "submit_code": ("Backend.helper.security.session_auth", "submit_code"),
    "submit_password": ("Backend.helper.security.session_auth", "submit_password"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib
        mod_name, attr = _LAZY[name]
        mod = importlib.import_module(mod_name)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

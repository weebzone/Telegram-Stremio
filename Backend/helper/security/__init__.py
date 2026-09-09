"""
security/ — crypto, password hashing, and userbot session auth helpers.

  - encrypt        encode_string / decode_string (compressed base62)
  - passwords      hash_password / verify_password
  - session_auth   Telegram userbot login / session lifecycle

FastAPI-layer auth (require_auth, verify_token) stays under
Backend.fastapi.security.
"""

from Backend.helper.security.encrypt import decode_string, encode_string
from Backend.helper.security.passwords import hash_password, is_hashed, verify_password
from Backend.helper.security.session_auth import (
    disconnect_session,
    get_active_session_string,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)

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

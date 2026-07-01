import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


#----- True if a stored value is already a PBKDF2 hash produced by hash_password
def is_hashed(stored: str) -> bool:
    return isinstance(stored, str) and stored.startswith(f"{_ALGO}$")


#----- Hash a plaintext password as pbkdf2_sha256$iterations$salt_hex$hash_hex
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


#----- Verify a password against a stored hash, with a constant-time plaintext fallback for legacy values
def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if not is_hashed(stored):
        return hmac.compare_digest(password or "", stored)
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False

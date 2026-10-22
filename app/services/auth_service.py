"""Admin login against the credentials from the environment (AuthSettings)."""
import hashlib
import hmac

from app.config import AuthSettings

START_PAGE = "/"


def credentials_match(auth: AuthSettings, username: str, password: str) -> bool:
    # compare_digest takes as long for a near miss as for a wild guess, so response times reveal
    # nothing. Bytes, because it rejects str with non-ASCII characters. Both are always compared.
    username_matches = hmac.compare_digest(username.encode(), auth.admin_username.encode())
    password_matches = hmac.compare_digest(password.encode(), auth.admin_password.encode())
    return username_matches and password_matches


def session_signing_key(auth: AuthSettings) -> str:
    """Key that signs the session cookie.

    Derived from the credentials as well as the secret, so changing the admin password logs out
    every open session. The secret keeps a stolen cookie from being used to guess the password.
    """
    credentials = f"{auth.admin_username}\n{auth.admin_password}".encode()
    return hmac.new(auth.session_secret.encode(), credentials, hashlib.sha256).hexdigest()


def safe_next_path(next_path: str | None) -> str:
    """Where to go after the login: a path on this site only, never another host (open redirect)."""
    if not next_path or not next_path.startswith("/"):
        return START_PAGE
    # Browsers read "//host" and "/\host" as another host, and drop tabs and newlines first.
    if next_path.startswith("//") or "\\" in next_path or any(ord(char) < 32 for char in next_path):
        return START_PAGE
    return next_path

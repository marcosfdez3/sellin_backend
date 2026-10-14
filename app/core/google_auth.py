"""
google_auth.py
──────────────
Verifica los ID tokens que la app obtiene con "Entrar con Google".
Las claves públicas de Google se descargan y se cachean (PyJWKClient).
"""
import jwt
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_jwks_client = jwt.PyJWKClient(GOOGLE_CERTS_URL, cache_keys=True)


class GoogleTokenError(Exception):
    pass


def _verify(token: str) -> dict:
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
        )
    except jwt.PyJWTError as e:
        raise GoogleTokenError(str(e)) from e

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleTokenError("Emisor no válido")
    if not claims.get("email") or not claims.get("email_verified"):
        raise GoogleTokenError("La cuenta de Google no tiene un email verificado")
    return claims


async def verify_google_id_token(token: str) -> dict:
    """Devuelve los claims del token (sub, email…) o lanza GoogleTokenError."""
    # PyJWKClient hace peticiones HTTP síncronas: fuera del event loop
    return await run_in_threadpool(_verify, token)

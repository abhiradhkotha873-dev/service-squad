from typing import Optional

import jwt
from jwt import PyJWKClient
from fastapi import Header, HTTPException

from .config import get_settings

_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(url: str) -> PyJWKClient:
    if url not in _jwks_clients:
        _jwks_clients[url] = PyJWKClient(url)
    return _jwks_clients[url]


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token header.")
    alg = header.get("alg", "")
    try:
        if alg == "HS256":
            if not settings.supabase_jwt_secret:
                raise HTTPException(
                    status_code=500,
                    detail="Server missing SUPABASE_JWT_SECRET for HS256 tokens.",
                )
            return jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
            )
        if alg in {"RS256", "ES256"}:
            jwks_url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
            client = _get_jwks_client(jwks_url)
            signing_key = client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=[alg],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired.")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    raise HTTPException(status_code=401, detail=f"Unsupported algorithm: {alg}")


async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_token(token)
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Token missing subject.")
    return claims

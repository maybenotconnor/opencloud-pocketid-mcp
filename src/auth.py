from fastmcp.server.auth.oidc_proxy import OIDCProxy

from src.config import settings


_INSECURE_JWT_DEFAULT = "change-me-to-a-random-secret"


def create_auth() -> OIDCProxy:
    """Create OIDCProxy auth configured for PocketID."""
    if not settings.jwt_signing_key or settings.jwt_signing_key == _INSECURE_JWT_DEFAULT:
        raise ValueError(
            "JWT_SIGNING_KEY is not set to a secure value. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return OIDCProxy(
        config_url=settings.pocketid_config_url,
        client_id=settings.pocketid_client_id,
        client_secret=settings.pocketid_client_secret,
        base_url=settings.mcp_base_url,
        jwt_signing_key=settings.jwt_signing_key,
        required_scopes=["openid", "profile", "email"],
        verify_id_token=True,
    )

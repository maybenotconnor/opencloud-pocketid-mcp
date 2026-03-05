from fastmcp.server.auth.oidc_proxy import OIDCProxy

from src.config import settings


def create_auth() -> OIDCProxy:
    """Create OIDCProxy auth configured for PocketID."""
    return OIDCProxy(
        config_url=settings.pocketid_config_url,
        client_id=settings.pocketid_client_id,
        client_secret=settings.pocketid_client_secret,
        base_url=settings.mcp_base_url,
        jwt_signing_key=settings.jwt_signing_key,
    )

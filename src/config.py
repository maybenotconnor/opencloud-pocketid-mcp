import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # PocketID OIDC
    pocketid_config_url: str = os.environ.get("POCKETID_CONFIG_URL", "")
    pocketid_client_id: str = os.environ.get("POCKETID_CLIENT_ID", "")
    pocketid_client_secret: str = os.environ.get("POCKETID_CLIENT_SECRET", "")

    # MCP Server
    mcp_base_url: str = os.environ.get("MCP_BASE_URL", "http://localhost:8000")
    mcp_port: int = int(os.environ.get("MCP_PORT", "8000"))

    # Security
    jwt_signing_key: str = os.environ.get("JWT_SIGNING_KEY", "change-me-to-a-random-secret")

    # OpenCloud instance
    opencloud_url: str = os.environ.get("OPENCLOUD_URL", "")
    opencloud_username: str = os.environ.get("OPENCLOUD_USERNAME", "")
    opencloud_password: str = os.environ.get("OPENCLOUD_PASSWORD", "")

    # Optional URL overrides (derived from OPENCLOUD_URL if not set)
    _webdav_url: str = os.environ.get("WEBDAV_URL", "")
    _caldav_url: str = os.environ.get("CALDAV_URL", "")
    _carddav_url: str = os.environ.get("CARDDAV_URL", "")

    @property
    def webdav_url(self) -> str:
        return self._webdav_url or (self.opencloud_url.rstrip("/") + "/remote.php/webdav")

    @property
    def caldav_url(self) -> str:
        return self._caldav_url or (self.opencloud_url.rstrip("/") + "/.well-known/caldav")

    @property
    def carddav_url(self) -> str:
        return self._carddav_url or (self.opencloud_url.rstrip("/") + "/.well-known/carddav")

    # Optional
    default_timezone: str = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")


settings = Settings()

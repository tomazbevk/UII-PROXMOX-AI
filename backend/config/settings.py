from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    app_env: str = Field("development")
    app_host: str = Field("0.0.0.0")
    app_port: int = Field(8000)

    proxmox_url: str = Field("")
    proxmox_host_ip: str | None = Field(default=None)
    proxmox_ip: str | None = Field(default=None)
    proxmox_node: str | None = Field(default=None)
    proxmox_port: int = Field(8006)
    proxmox_realm: str = Field("pve")
    proxmox_user: str = Field("ai-stack")
    proxmox_token_id: str = Field("assistant")
    proxmox_token_secret: str = Field(...)
    proxmox_password: str | None = Field(default=None)
    proxmox_otp: str | None = Field(default=None)
    proxmox_pve_auth_cookie: str | None = Field(default=None)
    proxmox_pve_csrf_token: str | None = Field(default=None)
    proxmox_verify_ssl: bool = Field(False)

    qdrant_url: str = Field(...)
    qdrant_api_key: str = Field("")
    qdrant_current_collection_name: str = Field(
        "infrastructure_current",
    )
    qdrant_history_collection_name: str = Field(
        "infrastructure_history",
    )

    ollama_url: str = Field(...)
    ollama_model: str = Field("llama3.1:8b")
    loki_url: str = Field(...)
    prometheus_url: str = Field(...)
    approval_db_path: str = Field("data/approvals.sqlite3")

    @property
    def proxmox_token_name(self) -> str:
        if "@" in self.proxmox_user:
            user_identity = self.proxmox_user
        else:
            user_identity = f"{self.proxmox_user}@{self.proxmox_realm}"
        return f"{user_identity}!{self.proxmox_token_id}"

    @property
    def proxmox_auth_header(self) -> str:
        return f"PVEAPIToken={self.proxmox_token_name}={self.proxmox_token_secret}"

    @property
    def proxmox_api_base_url(self) -> str:
        host_ip = self.proxmox_host_ip or self.proxmox_ip
        if host_ip:
            if host_ip.startswith(("http://", "https://")):
                parsed = urlparse(host_ip)
                host_ip = parsed.netloc or parsed.path
            host_ip = host_ip.rstrip("/")
            # Proxmox always serves HTTPS on the API port; verify_ssl only
            # controls whether the TLS certificate is validated, not the scheme.
            return f"https://{host_ip}:{self.proxmox_port}"
        if self.proxmox_url:
            parsed = urlparse(self.proxmox_url)
            if parsed.scheme and parsed.netloc:
                netloc = parsed.netloc.rstrip("/")
                if ":" not in netloc.rsplit("]", 1)[-1]:
                    netloc = f"{netloc}:{self.proxmox_port}"
                return parsed._replace(netloc=netloc, path=parsed.path.rstrip("/")).geturl().rstrip("/")
            return self.proxmox_url.rstrip("/")
        raise ValueError("Either proxmox_host_ip, proxmox_ip, or proxmox_url must be configured")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

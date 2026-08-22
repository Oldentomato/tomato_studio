import os

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOMATO_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="sqlite:///./data/studio.db",
        validation_alias=AliasChoices("DATABASE_URL", "TOMATO_DATABASE_URL", "database_url"),
    )
    docker_image: str = "codercom/code-server:latest"
    docker_host: str = ""
    docker_ssh_password: str = ""
    max_running: int = 2
    idle_timeout_seconds: int = 30 * 60
    workspace_bind_ip: str = "127.0.0.1"
    public_workspace_host: str = "localhost"
    studio_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    mem_limit: str = "2g"
    ready_timeout_seconds: int = 90
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.studio_origins.split(",") if item.strip()]

    @property
    def resolved_openai_key(self) -> str:
        return self.openai_api_key or os.environ.get("OPENAI_API_KEY", "")


settings = Settings()

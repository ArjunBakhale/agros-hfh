from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Every live-side-effect gate defaults closed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agros_app:agros_app@localhost:55432/agros"
    admin_database_url: str | None = None
    app_db_password: str | None = None

    session_secret: str = Field(default="dev-only-session-secret-change-me-32bytes!!")
    public_base_url: str = "http://localhost:8000"
    webhook_base_url: str = "http://localhost:8000"
    seed_admin_email: str = "admin@agros-demo.test"
    seed_password: str | None = None
    app_env: Literal["local", "test", "aws"] = "local"
    cookie_secure: bool = False

    voice_agent_enabled: bool = False
    voice_agent_provider: Literal["fixture", "livekit"] = "fixture"
    voice_live_calls_approved: bool = False
    voice_live_call_allowlist: str = ""
    voice_real_contacts_approved: bool = False
    voice_call_recording_enabled: bool = False
    voice_transcript_retention_enabled: bool = False
    voice_webhook_secret: str = Field(default="dev-only-webhook-secret-change-me-32bytes!!")
    voice_webhook_tolerance_seconds: int = 300
    voice_call_timeout_minutes: int = 15
    voice_enforce_call_window: bool = True

    classifier_mode: Literal["rules", "bedrock"] = "rules"
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "us.amazon.nova-lite-v1:0"

    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    livekit_sip_trunk_id: str | None = None
    livekit_agent_name: str = "agros-outreach"
    twilio_phone_number: str | None = None
    deepgram_api_key: str | None = None
    cartesia_api_key: str | None = None
    cartesia_voice_id: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

    # Discrete DB parts (as injected by ECS from Secrets Manager); override the URLs when set.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "agros"
    db_master_user: str | None = None
    db_master_password: str | None = None
    db_sslmode: str = "require"

    @model_validator(mode="after")
    def _compose_db_urls(self) -> Settings:
        if self.db_host and self.app_db_password:
            host = f"{self.db_host}:{self.db_port}/{self.db_name}"
            self.database_url = f"postgresql://agros_app:{quote(self.app_db_password, safe='')}@{host}?sslmode={self.db_sslmode}"
            if self.db_master_user and self.db_master_password:
                self.admin_database_url = (
                    f"postgresql://{quote(self.db_master_user, safe='')}:{quote(self.db_master_password, safe='')}@{host}"
                    f"?sslmode={self.db_sslmode}"
                )
        return self

    def live_allowlist(self) -> set[str]:
        return {n.strip() for n in self.voice_live_call_allowlist.split(",") if n.strip()}

    def livekit_configured(self) -> bool:
        return all(
            [
                self.livekit_url,
                self.livekit_api_key,
                self.livekit_api_secret,
                self.livekit_sip_trunk_id,
            ]
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

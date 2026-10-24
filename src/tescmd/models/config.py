from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(BaseModel):
    """A named profile grouping common CLI settings."""

    region: str = "na"
    vin: str | None = None
    output_format: str | None = None
    client_id: str | None = None
    client_secret: str | None = None


class AppSettings(BaseSettings):
    """Application-wide settings populated from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TESLA_",
        extra="ignore",
    )

    client_id: str | None = None
    client_secret: str | None = None
    domain: str | None = None
    vin: str | None = None

    @field_validator("domain", mode="before")
    @classmethod
    def _lowercase_domain(cls, v: str | None) -> str | None:
        """Tesla Fleet API rejects domains with uppercase characters."""
        if v is not None:
            return v.lower()
        return v

    region: str = "na"
    token_file: str | None = None
    config_dir: str = "~/.config/tescmd"
    output_format: str | None = None
    profile: str = "default"
    setup_tier: str | None = None
    github_repo: str | None = None
    hosting_method: str | None = None  # "github" | "tailscale" | None (manual)
    access_token: str | None = None
    refresh_token: str | None = None

    # Cache settings (TESLA_CACHE_ENABLED, TESLA_CACHE_TTL, TESLA_CACHE_DIR)
    cache_enabled: bool = True
    cache_ttl: int = 60
    cache_dir: str = "~/.cache/tescmd"

    # Unsigned REST commands are available only through explicit opt-in.
    command_protocol: Literal["auto", "signed", "unsigned"] = "auto"

    # Display units (TESLA_TEMP_UNIT, TESLA_DISTANCE_UNIT, TESLA_PRESSURE_UNIT)
    temp_unit: str = "F"
    distance_unit: str = "mi"
    pressure_unit: str = "psi"

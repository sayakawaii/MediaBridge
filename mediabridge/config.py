"""Configuration loading, ``${VAR}`` interpolation and validation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_DESC_TEMPLATE = """{description}

――――――――――
本作品为转载，版权归原作者所有。
原作者：{author}
原始链接：{webpage_url}
许可协议：{license}
由 MediaBridge 自动搬运。"""


def expand_env_vars(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` from the environment.

    An unset variable is left as the literal ``${VAR}`` rather than collapsing
    to an empty string, so a typo or a missing secret fails loudly downstream
    instead of silently producing a blank field.
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    return value


class AcFunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cookie_env: str = "ACFUN_COOKIE"
    """Name of the environment variable holding the cookie blob (not the value)."""

    cookie_file: str | None = None
    """Local fallback path, used when the environment variable is absent."""

    request_timeout: int = 60
    upload_timeout: int = 300


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_filesize_mb: int = 2000
    """GitHub-hosted runners only guarantee 14 GB of free disk."""

    max_duration_sec: int = 3600
    min_duration_sec: int = 0
    max_items_per_run: int = 3
    max_items_per_source: int = 2


class PublishConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = "acfun_video"
    channel_id: int | None = None
    realm_id: int | None = None

    creation_type: int = 1
    """1 = 转载 (repost), 3 = 原创 (original)."""

    tags: list[str] = Field(default_factory=list)
    title_template: str = "【搬运】{title}"
    desc_template: str = DEFAULT_DESC_TEMPLATE
    original_declare: bool = False

    @field_validator("creation_type")
    @classmethod
    def _check_creation_type(cls, v: int) -> int:
        if v not in (1, 3):
            raise ValueError("creation_type must be 1 (转载) or 3 (原创)")
        return v


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    """Dedup namespace. Renaming a source re-publishes everything it finds."""

    type: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    publish: PublishConfig

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", v):
            raise ValueError("source name may only contain letters, digits, '.', '_' and '-'")
        return v


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    acfun: AcFunConfig = Field(default_factory=AcFunConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    state_file: str = "state/published.json"
    work_dir: str = "work"

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def load_config(path: str | Path) -> Config:
    """Read, interpolate and validate a YAML or JSON config file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}",
            hint="Copy config.example.yaml to config.yaml and edit it.",
        )

    raw_text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(raw_text)
        else:
            data = yaml.safe_load(raw_text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    data = expand_env_vars(data)

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Configuration validation failed for {path}\n{exc}") from exc

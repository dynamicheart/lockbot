"""Non-secret browser data-source settings; never fetch the endpoint server-side."""

import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UtilizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,64}$")
    name: str = Field(default="Prometheus", min_length=1, max_length=64)
    type: str = "prometheus"
    profile: str = "custom"
    accessMode: str = "independent"
    endpoint: str = Field(max_length=2048)
    nodeLabel: str = "instance"
    deviceLabel: str = "UUID"
    utilizationMetric: str = "DCGM_FI_DEV_GPU_UTIL"
    memoryUsedMetric: str = "DCGM_FI_DEV_FB_USED"
    memoryTotalMetric: str = "DCGM_FI_DEV_FB_TOTAL"
    memoryUnit: str = "MiB"
    nodeMapping: dict[str, str] = Field(min_length=1, max_length=512)
    deviceMapping: dict[str, str] = Field(default_factory=dict, max_length=512)
    intervalSeconds: int = Field(default=30, ge=30, le=3600)
    staleSeconds: int = Field(default=90, ge=30, le=3600)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Use an HTTP(S) URL without credentials, query or fragment")
        if parsed.port is not None and parsed.port == 0:
            raise ValueError("Invalid data source port")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_metrics(self):
        if self.accessMode not in {"independent", "short_lived"}:
            raise ValueError("Unsupported metrics access mode")
        if self.profile not in {"custom", "lockbot-v1"}:
            raise ValueError("Unsupported telemetry profile")
        if self.profile == "lockbot-v1":
            self.nodeLabel, self.deviceLabel, self.memoryUnit = "node", "device", "B"
            self.utilizationMetric = "lockbot_accelerator_utilization_percent"
            self.memoryUsedMetric = "lockbot_accelerator_memory_used_bytes"
            self.memoryTotalMetric = "lockbot_accelerator_memory_total_bytes"
        if self.type != "prometheus" or self.memoryUnit not in {"B", "MiB"}:
            raise ValueError("Unsupported source or memory unit")
        for label in (self.nodeLabel, self.deviceLabel):
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", label) or label.startswith("__"):
                raise ValueError("Invalid identity label")
        if self.nodeLabel == self.deviceLabel:
            raise ValueError("Identity labels must differ")
        for metric in (self.utilizationMetric, self.memoryUsedMetric, self.memoryTotalMetric):
            if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]{0,255}", metric):
                raise ValueError("Use a metric name, not arbitrary PromQL")
        for mapping in (self.nodeMapping, self.deviceMapping):
            if any(not k or len(k) > 256 or not v or len(v) > 256 for k, v in mapping.items()):
                raise ValueError("Invalid resource mapping")
        return self


def public_datasource(bot):
    """Return only the validated non-secret schema, including for read-only views."""
    try:
        return (
            UtilizationConfig.model_validate_json(bot.utilization_config).model_dump()
            if bot.utilization_config
            else None
        )
    except (ValueError, TypeError):
        return None


def serialized_datasource(config):
    return json.dumps(config.model_dump(), ensure_ascii=False) if config is not None else None

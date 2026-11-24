"""Validate and merge hardware observations uploaded by configuration credentials."""

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from lockbot.backend.app.bots.node_hardware import WHOLE_MACHINE_TYPES, saved_node_hardware

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ByteCount = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
VersionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
DeviceIndex = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,3})$")]


class AcceleratorInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ShortText
    count: int = Field(strict=True, ge=1, le=256)
    memory_bytes_per_card: ByteCount | None = None

    @field_validator("model")
    @classmethod
    def printable_model(cls, model):
        if not model.isprintable():
            raise ValueError("Invalid accelerator model")
        return model


class DeviceFirmware(BaseModel):
    model_config = ConfigDict(extra="forbid")

    firmware_version: VersionText | None = None
    sbl_version: VersionText | None = None

    @field_validator("firmware_version", "sbl_version")
    @classmethod
    def printable_version(cls, version):
        if version is not None and not version.isprintable():
            raise ValueError("Invalid firmware version")
        return version


class NodeSoftware(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_version: VersionText | None = None
    device_firmware: dict[DeviceIndex, DeviceFirmware] = Field(default_factory=dict, max_length=256)

    @field_validator("driver_version")
    @classmethod
    def printable_driver(cls, version):
        if version is not None and not version.isprintable():
            raise ValueError("Invalid driver version")
        return version


class NodeInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: AwareDatetime
    scope: Literal["host", "container", "unknown"]
    accelerators: list[AcceleratorInventory] = Field(min_length=1, max_length=256)
    memory_bytes: ByteCount | None = None
    cpu_arch: ShortText | None = None
    software: NodeSoftware | None = None


class InventoryUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    nodes: dict[str, NodeInventory] = Field(min_length=1, max_length=512)


def inventory_document(bot, node_ids):
    nodes = json.loads(bot.node_inventory_config or "{}")
    return {"version": 1, "nodes": {node: nodes[node] for node in node_ids if node in nodes}}


def saved_node_observation(bot, node):
    try:
        record = inventory_document(bot, [node])["nodes"].get(node)
        return NodeInventory.model_validate(record) if record is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def resource_node_hardware(bot):
    """Use whole-node host observations, falling back to earlier manual registrations."""
    if bot.bot_type not in WHOLE_MACHINE_TYPES:
        return {}
    hardware = saved_node_hardware(bot)
    try:
        observations = json.loads(bot.node_inventory_config or "{}")
    except (TypeError, ValueError):
        return hardware
    if not isinstance(observations, dict):
        return hardware
    for node, observation in observations.items():
        if not isinstance(observation, dict) or observation.get("scope") != "host":
            continue
        try:
            accelerators = NodeInventory.model_validate(observation).accelerators
        except ValueError:
            continue
        by_model = {}
        for accelerator in accelerators:
            row = by_model.setdefault(accelerator.model.casefold(), {"model": accelerator.model, "count": 0})
            row["count"] += accelerator.count
        hardware[node] = list(by_model.values())
    return hardware


def update_inventory(raw, previous, node_ids):
    updates = InventoryUpdates.model_validate(raw)
    if set(updates.nodes) - set(node_ids):
        raise ValueError("Unknown node")
    current = dict(previous["nodes"])
    latest = datetime.now(timezone.utc) + timedelta(minutes=2)
    for node, observation in updates.nodes.items():
        if observation.observed_at > latest:
            raise ValueError("Observation is in the future")
        if node in current:
            saved = NodeInventory.model_validate(current[node])
            if observation.observed_at <= saved.observed_at:
                continue
            if saved.scope == "host" and observation.scope != "host":
                continue
        fields = {"software"} if observation.software is None else set()
        current[node] = observation.model_dump(mode="json", exclude=fields)
    return {"version": 1, "nodes": current}

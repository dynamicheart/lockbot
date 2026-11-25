"""Validate and merge hardware observations uploaded by configuration credentials."""

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from lockbot.backend.app.bots.node_hardware import WHOLE_MACHINE_TYPES, saved_node_hardware

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ByteCount = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]


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


AttributeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class InventoryAttribute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: ShortText
    value: AttributeText
    unit: Annotated[ShortText, StringConstraints(max_length=24)] | None = None

    @field_validator("label", "value", "unit")
    @classmethod
    def printable(cls, value):
        if value is not None and not value.isprintable():
            raise ValueError("Inventory attributes must be single-line text")
        return value


class DeviceAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(strict=True, ge=0, le=255)
    attributes: list[InventoryAttribute] = Field(min_length=1, max_length=24)


class NodeInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: AwareDatetime
    scope: Literal["host", "container", "unknown"]
    accelerators: list[AcceleratorInventory] = Field(min_length=1, max_length=256)
    memory_bytes: ByteCount | None = None
    cpu_arch: ShortText | None = None
    attributes: list[InventoryAttribute] = Field(default_factory=list, max_length=24)
    devices: list[DeviceAttributes] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_attributes(self):
        if len({device.index for device in self.devices}) != len(self.devices):
            raise ValueError("Duplicate device index")
        if len(self.devices) > sum(card.count for card in self.accelerators):
            raise ValueError("More device entries than observed accelerators")
        fields = self.model_dump_json(include={"attributes", "devices"}, exclude_none=True)
        if len(fields.encode()) > 4096:
            raise ValueError("Node attributes exceed 4096 bytes")
        return self


class InventoryUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    nodes: dict[str, NodeInventory] = Field(min_length=1, max_length=512)


def inventory_document(bot, node_ids):
    nodes = json.loads(bot.node_inventory_config or "{}")
    return {"version": 1, "nodes": {node: nodes[node] for node in node_ids if node in nodes}}


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
        # Preserve the original shape of older uploads when no attributes are supplied.
        stored = observation.model_dump(mode="json", exclude={"attributes", "devices"})
        stored.update(
            observation.model_dump(
                mode="json", include=observation.model_fields_set & {"attributes", "devices"}, exclude_none=True
            )
        )
        current[node] = stored
    return {"version": 1, "nodes": current}

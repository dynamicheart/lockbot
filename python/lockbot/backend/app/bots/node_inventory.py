"""Validate and merge hardware observations uploaded by configuration credentials."""

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ByteCount = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]


class AcceleratorInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ShortText
    count: int = Field(strict=True, ge=1, le=256)
    memory_bytes_per_card: ByteCount | None = None


class NodeInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: AwareDatetime
    scope: Literal["host", "container", "unknown"]
    accelerators: list[AcceleratorInventory] = Field(min_length=1, max_length=256)
    memory_bytes: ByteCount | None = None
    cpu_arch: ShortText | None = None


class InventoryUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    nodes: dict[str, NodeInventory] = Field(min_length=1, max_length=512)


def inventory_document(bot, node_ids):
    nodes = json.loads(bot.node_inventory_config or "{}")
    return {"version": 1, "nodes": {node: nodes[node] for node in node_ids if node in nodes}}


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
        current[node] = observation.model_dump(mode="json")
    return {"version": 1, "nodes": current}

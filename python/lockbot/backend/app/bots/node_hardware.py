"""Validated, optional accelerator inventory for whole-machine groups."""

import json
from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from lockbot.backend.app.bots.node_access import _unique_object, node_ids

WHOLE_MACHINE_TYPES = frozenset({"NODE", "QUEUE"})
_MAX_IMPORT_BYTES = 256 * 1024
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
NodeId = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Accelerator(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: ModelName
    count: int = Field(ge=1)


class NodeHardwareMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(ge=1, le=1)
    nodes: dict[NodeId, list[Accelerator]]

    @model_validator(mode="after")
    def check_models(self):
        for accelerators in self.nodes.values():
            models = {accelerator.model.casefold() for accelerator in accelerators}
            if len(models) != len(accelerators):
                raise ValueError("Duplicate accelerator model on a node")
            if any(not accelerator.model.isprintable() for accelerator in accelerators):
                raise ValueError("Invalid accelerator model")
        return self


def parse_node_hardware(raw):
    data = json.loads(raw, object_pairs_hook=_unique_object)
    return NodeHardwareMapping.model_validate(data).model_dump()["nodes"]


async def read_node_hardware(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_IMPORT_BYTES:
            raise HTTPException(413, detail="node_hardware_mapping_too_large")
        body.extend(chunk)
    try:
        return parse_node_hardware(body)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, detail="node_hardware_invalid_mapping") from None


def saved_node_hardware(bot):
    if not bot.node_hardware_config or bot.bot_type not in WHOLE_MACHINE_TYPES:
        return {}
    try:
        allowed = set(node_ids(json.loads(bot.cluster_configs)))
        saved = parse_node_hardware(bot.node_hardware_config)
    except (TypeError, ValueError):
        return {}
    return {node: rows for node, rows in saved.items() if node in allowed and rows}


def serialize_node_hardware(nodes):
    return json.dumps({"version": 1, "nodes": nodes}, ensure_ascii=False) if nodes else None

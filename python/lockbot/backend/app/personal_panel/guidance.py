"""Read-only references from deployment-owned configuration."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from lockbot.backend.app.bots.metrics_signers import canonical_audience
from lockbot.backend.app.bots.node_access import _active_holding
from lockbot.backend.app.database import get_db
from lockbot.backend.app.resource_auth.dependencies import require_resource_actor

PROTOCOL = "lockbot.resource-guidance.v1"
GUIDANCE_ID = re.compile(r"[a-f0-9]{32}\Z")


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    instance_id: str = Field(min_length=1, max_length=64)
    bot_uuid: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=32)
    realm: str = Field(min_length=1, max_length=512)
    node: str = Field(min_length=1, max_length=256)
    device: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    documentation_url: str | None = Field(default=None, max_length=2048)
    operator_refs: list[str] | None = Field(default=None, min_length=1, max_length=64)

    def descriptor(self):
        return {
            "protocol": PROTOCOL,
            "id": self.id,
            "revision": self.revision,
            "title": self.title,
            "node": self.node,
            "device": self.device,
        }

    def read(self):
        return {
            **self.descriptor(),
            "summary": self.summary,
            "documentation_url": self.documentation_url,
        }


@dataclass(frozen=True, repr=False)
class Guidance:
    references: tuple[Reference, ...]

    @classmethod
    def configured(cls, config):
        if not isinstance(config, dict) or set(config) != {"documentation_origins", "references"}:
            raise ValueError("Invalid resource guidance configuration")
        origins, entries = config["documentation_origins"], config["references"]
        if not isinstance(origins, list) or len(origins) > 16 or not isinstance(entries, list) or len(entries) > 128:
            raise ValueError("Invalid resource guidance configuration")
        for origin in origins:
            if not canonical_audience(origin).startswith("https://") or urlsplit(origin).path:
                raise ValueError("Resource guidance requires exact HTTPS documentation origins")
        references = tuple(Reference.model_validate(item) for item in entries)
        ids = set()
        for reference in references:
            if (
                reference.id in ids
                or not reference.title.strip()
                or not reference.title.isprintable()
                or not reference.summary.strip()
                or any(not char.isprintable() and char != "\n" for char in reference.summary)
                or reference.operator_refs is not None
                and any(not ref or len(ref) > 128 for ref in reference.operator_refs)
            ):
                raise ValueError("Invalid resource guidance reference")
            ids.add(reference.id)
            if reference.documentation_url is not None:
                url = canonical_audience(reference.documentation_url)
                parts = urlsplit(url)
                if f"{parts.scheme}://{parts.netloc}" not in origins:
                    raise ValueError("Unpinned resource guidance link")
        return cls(references)

    @staticmethod
    def discover(groups):
        return [
            item
            for group in groups
            if group["status"] == "ok"
            for allocation in group["allocations"]
            for item in allocation.get("resource_guidance", ())
        ]

    def attach(self, resources, instance, bot_uuid, grant, now):
        matches = {}
        for reference in self.references:
            if (reference.instance_id, reference.bot_uuid, reference.platform, reference.realm) != (
                instance,
                bot_uuid,
                grant["platform"],
                grant["realm"],
            ):
                continue
            if reference.operator_refs is None or grant["ref"] in reference.operator_refs:
                matches.setdefault((reference.node, reference.device), []).append(reference)
        for node in resources:
            for unit in node.get("devices", [node]):
                device = str(unit["id"]) if unit is not node else None
                eligible = matches.get((node["id"], device))
                if eligible and _active_holding(unit, grant["subject"], now):
                    unit["resource_guidance"] = [reference.descriptor() for reference in eligible]

    def read(self, groups, reference_id):
        if not any(item["id"] == reference_id for item in self.discover(groups)):
            return None
        return next((reference.read() for reference in self.references if reference.id == reference_id), None)


def mount_agent_guidance(app, provider):
    from .native import ProviderError

    def current_groups(request, actor, db):
        from lockbot.backend.app.resource_auth.dependencies import reverify_actor
        from lockbot.backend.app.resource_auth.models import ResourceBinding

        if not reverify_actor(db, request, actor):
            raise ProviderError("credential_invalid")
        grant = provider._grant(db, db.get(ResourceBinding, actor.binding_record_id))
        if grant["ref"] != actor.binding_public_id:
            raise HTTPException(404)
        return provider.report(grant)

    @app.get("/api/bots/{bot_id}/resource-guidance", include_in_schema=False)
    @app.get("/api/bots/{bot_id}/resource-guidance/{reference_id}", include_in_schema=False)
    def resource_guidance(
        request: Request,
        response: Response,
        reference_id: str | None = None,
        actor=Depends(require_resource_actor),
        db=Depends(get_db),
    ):
        from lockbot.backend.app.resource_api.traffic import credential_budget

        headers = {"Cache-Control": "no-store", "Vary": "Authorization"}
        response.headers.update(headers)
        if str(request.base_url).rstrip("/") != provider.config["issuer"]:
            raise HTTPException(404, headers=headers)
        if reference_id is not None and not GUIDANCE_ID.fullmatch(reference_id):
            raise HTTPException(404, headers=headers)
        allowed, retry = credential_budget.check(actor.binding_record_id, "read")
        if not allowed:
            raise HTTPException(429, headers={**headers, "Retry-After": str(retry)})
        try:
            groups = current_groups(request, actor, db)
        except ProviderError:
            raise HTTPException(404, headers=headers) from None
        if reference_id is not None:
            result = provider.guidance.read(groups, reference_id)
            if result is None:
                raise HTTPException(404, headers=headers)
            return result
        return {"protocol": PROTOCOL, "items": provider.guidance.discover(groups)}

"""Observe low utilization for committed exclusive holdings and opt-in recovery."""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from lockbot.backend.app.bots.datasource import agent_metrics_source
from lockbot.backend.app.bots.manager import AdmissionFull, BotNotRunning, InstanceRetiring
from lockbot.backend.app.bots.metrics_access import issue_metrics_access
from lockbot.backend.app.bots.models import Bot
from lockbot.backend.app.database import SessionLocal
from lockbot.backend.app.resource_api.idle_notifications import IdleChat
from lockbot.backend.app.resource_api.idle_recovery import IdleRecovery
from lockbot.metrics.gateway import regex_escape

logger = logging.getLogger(__name__)

CONFIG_ENV = "LOCKBOT_IDLE_OBSERVATION_CONFIG"
IDLE_SECONDS = 20 * 60
POLL_SECONDS = 60
REQUEST_SECONDS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
UTILIZATION = "lockbot_accelerator_utilization_percent"
OBSERVED_AT = "lockbot_sample_timestamp_seconds"
COLLECTOR_OK = "lockbot_collector_success"
SourceNode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
Resource = tuple[int, str, int | None]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_id: int = Field(gt=0)
    bot_uuid: UUID
    node: str = Field(min_length=1)
    source_node: SourceNode
    cards: dict[int, str] = Field(default_factory=dict)
    recover_idle: bool = False


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_bot_metrics_grant: bool = False
    endpoint: AnyHttpUrl
    token_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    nodes: dict[SourceNode, list[str]] = Field(min_length=1)
    targets: list[Target] = Field(min_length=1)
    threshold_percent: float = Field(default=5, gt=0, le=100)
    max_sample_age_seconds: int = Field(default=240, ge=30, le=3600)

    @model_validator(mode="after")
    def check_scope(self):
        if any(
            value is not None
            for value in (self.endpoint.username, self.endpoint.password, self.endpoint.query, self.endpoint.fragment)
        ):
            raise ValueError("Metrics endpoint must contain only the service URL")
        if self.use_bot_metrics_grant:
            bots = {(target.bot_id, target.bot_uuid) for target in self.targets}
            if len(bots) != 1 or self.token_env or len(self.nodes) > 64:
                raise ValueError("Gateway batches require one bot, at most 64 nodes and no static token")
        for devices in self.nodes.values():
            if not devices or not all(devices) or len(devices) != len(set(devices)):
                raise ValueError("Each monitored node requires an explicit device inventory")
        for target in self.targets:
            inventory = self.nodes.get(target.source_node)
            if inventory is None or not set(target.cards.values()) <= set(inventory):
                raise ValueError("Target must map within the source inventory")
            if any(index < 0 for index in target.cards) or len(set(target.cards.values())) != len(target.cards):
                raise ValueError("Target cards must map to distinct source devices")
        return self

    @property
    def query(self):
        names = f"{UTILIZATION}|{OBSERVED_AT}|{COLLECTOR_OK}"
        selected = dict.fromkeys(target.source_node for target in self.targets)
        nodes = "|".join(node.replace(".", r"\.") for node in selected)
        if self.use_bot_metrics_grant:
            labels = "|".join(regex_escape(node) for node in selected)
            names = f"^(?:{names})$"
            nodes = f"^(?:{labels})$"
        return f'{{__name__=~"{names}",node=~{json.dumps(nodes)}}}'


def _bot_metrics_token(source):
    first = source.targets[0]
    with SessionLocal() as db:
        bot = db.get(Bot, first.bot_id)
        if bot is None or bot.is_deleted or bot.uuid != str(first.bot_uuid):
            return None
        live = agent_metrics_source(bot)
        if not live or live["endpoint"] != str(source.endpoint).rstrip("/"):
            return None
        for target in source.targets:
            if live["nodeMapping"].get(target.node) != target.source_node:
                return None
            if any(
                live["deviceMapping"].get(f"{target.node}:dev{idx}") != label for idx, label in target.cards.items()
            ):
                return None
        grant = issue_metrics_access(db, bot, live)
        return grant["token"] if grant else None


class ObservationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique_targets(self):
        keys = [(target.bot_id, target.node) for source in self.sources for target in source.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Each bot node must use one observation source")
        return self


@dataclass(frozen=True, slots=True)
class IdleEvidence:
    version: str
    since: float
    observed_at: float
    holder: str
    channel: str
    generation: int

    @property
    def candidate(self):
        return self.observed_at - self.since >= IDLE_SECONDS


def _metric_values(payload, nodes):
    if not isinstance(payload, dict) or payload.get("status") != "success" or payload.get("warnings"):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
        return {}
    values = defaultdict(dict)
    for row in data["result"]:
        labels = row.get("metric") if isinstance(row, dict) else None
        if not isinstance(labels, dict):
            return {}
        node, metric = labels.get("node"), labels.get("__name__")
        if not isinstance(node, str) or node not in nodes:
            continue
        if metric == UTILIZATION:
            device = labels.get("device")
            if not isinstance(device, str) or not device:
                continue
        elif metric in (OBSERVED_AT, COLLECTOR_OK) and labels.get("collector") == "accelerator":
            device = None
        else:
            continue
        samples = values[node]
        key = metric, device
        try:
            raw = row["value"][1]
            value = float(raw) if isinstance(raw, str) and key not in samples else None
        except (IndexError, KeyError, TypeError, ValueError):
            value = None
        samples[key] = value if value is not None and isfinite(value) else None
    return values


def _advance(previous, version, holder, now, metrics, devices, source, generation):
    observed_at = metrics.get((OBSERVED_AT, None))
    if (
        metrics.get((COLLECTOR_OK, None)) != 1
        or observed_at is None
        or not holder["start_time"] <= observed_at <= now
        or now - observed_at > source.max_sample_age_seconds
    ):
        return None
    for device in devices:
        value = metrics.get((UTILIZATION, device))
        if value is None or not 0 <= value < source.threshold_percent:
            return None
    since = observed_at
    if previous is not None and previous.version == version:
        if observed_at < previous.observed_at:
            return None
        if observed_at - previous.observed_at <= source.max_sample_age_seconds:
            since = previous.since
    return IdleEvidence(version, since, observed_at, holder["user_id"], holder.get("source", "unknown"), generation)


def _scoped_holdings(bot, target, state, inventory, now):
    node = state.get(target.node)
    if node is None:
        return
    if isinstance(node, tuple):
        units = (
            (index, node[index], (device,))
            for index, device in target.cards.items()
            if index < len(node) and node[index].get("dev_id") == index
        )
    else:
        units = ((None, node, inventory),)
    for index, unit, devices in units:
        holders = unit.get("current_users", ())
        if unit.get("status") == "exclusive" and len(holders) == 1:
            holder = holders[0]
            version = bot.op_holding_version(state, target.node, index, holder["user_id"], at=now)
            if version is not None:
                yield (target.bot_id, target.node, index), version, holder, devices


class IdleObserver:
    def __init__(self, config, manager, *, clock=time.time):
        self.sources = config.sources
        self.manager = manager
        self.clock = clock
        self._windows = [{} for _ in self.sources]
        self._targets_by_bot = []
        self._recovery_targets = []
        for source in self.sources:
            groups = defaultdict(list)
            for target in source.targets:
                groups[target.bot_id].append(target)
            self._targets_by_bot.append(groups)
            self._recovery_targets.append(
                {(target.bot_id, target.node) for target in source.targets if target.recover_idle}
            )
        self.recovery = IdleRecovery(manager, clock=clock) if any(self._recovery_targets) else None
        chat = IdleChat(manager, self.recovery, clock)
        self.chat_notification = chat.send
        self.confirm_chat = chat.confirm if self.recovery is not None else None
        self._network_slots = asyncio.Semaphore(4)

    @property
    def candidates(self):
        return {key: evidence for windows in self._windows for key, evidence in windows.items() if evidence.candidate}

    async def _fetch(self, source, client):
        headers = {}
        method = "GET"
        payload = {"params": {"query": source.query}}
        if source.use_bot_metrics_grant or source.token_env:
            if source.use_bot_metrics_grant:
                token = await asyncio.to_thread(_bot_metrics_token, source)
                method = "POST"
                payload = {"data": {"query": source.query, "timeout": "4s"}}
            else:
                token = os.environ.get(source.token_env)
            if not token:
                return {}
            headers = {"Authorization": f"Bearer {token}"}
        url = str(source.endpoint).rstrip("/") + "/api/v1/query"
        async with client.stream(method, url, headers=headers, **payload) as reply:
            reply.raise_for_status()
            body = bytearray()
            async for chunk in reply.aiter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    return {}
        return _metric_values(json.loads(body), source.nodes)

    async def poll_source(self, index, client):
        source = self.sources[index]
        previous = self._windows[index]
        async with self._network_slots:
            try:
                metrics = await asyncio.wait_for(self._fetch(source, client), timeout=REQUEST_SECONDS)
            except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
                metrics = {}
        now = self.clock()
        current = {}
        if metrics:
            for bot_id, targets in self._targets_by_bot[index].items():
                try:
                    with self.manager.admit(bot_id, for_read=True) as lease:
                        if lease.health != "error":
                            self._observe_bot(lease, targets, source, metrics, now, previous, current)
                except (AdmissionFull, BotNotRunning, InstanceRetiring):
                    continue
        self._windows[index] = current
        if (count := sum(evidence.candidate for evidence in current.values())) != sum(
            evidence.candidate for evidence in previous.values()
        ):
            logger.info("Low-utilization observation source %d has %d candidates", index + 1, count)
        if self.recovery is not None and self._recovery_targets[index]:
            evidence = {key: row for key, row in current.items() if key[:2] in self._recovery_targets[index]}
            created = self.recovery.reconcile(index, evidence)
            if self.chat_notification is not None:
                for notice in created:
                    if notice.channel == "chat" and await asyncio.to_thread(self.chat_notification, notice, "warning"):
                        self.recovery.delivered(notice)
            reclaimed = await asyncio.to_thread(self.recovery.reclaim_ready, evidence, source.max_sample_age_seconds)
            if self.chat_notification is not None:
                for notice in reclaimed:
                    await asyncio.to_thread(self.chat_notification, notice, "released")

    def _observe_bot(self, lease, targets, source, metrics, now, previous, current):
        bot = lease.bot
        state = bot.published_snapshot().state
        bot_uuid = bot.config.get_val("_LIFECYCLE_BOT_UUID")
        for target in targets:
            samples = metrics.get(target.source_node)
            if not samples or bot_uuid != str(target.bot_uuid):
                continue
            inventory = source.nodes[target.source_node]
            if target.recover_idle and any(
                (value := samples.get((UTILIZATION, device))) is None or not 0 <= value <= 100 for device in inventory
            ):
                continue
            for key, version, holder, devices in _scoped_holdings(bot, target, state, inventory, now):
                if key[2] is None and any(
                    metric == UTILIZATION and device not in devices for metric, device in samples
                ):
                    continue
                evidence = _advance(previous.get(key), version, holder, now, samples, devices, source, lease.generation)
                if evidence is not None:
                    current[key] = evidence

    async def _source_loop(self, index, client):
        while True:
            try:
                await self.poll_source(index, client)
            except Exception as error:
                self._windows[index] = {}
                if self.recovery is not None:
                    self.recovery.reconcile(index, {})
                logger.error("Low-utilization observation source %d failed (%s)", index + 1, type(error).__name__)
            await asyncio.sleep(POLL_SECONDS)

    async def run(self):
        async with httpx.AsyncClient(timeout=REQUEST_SECONDS, trust_env=False, follow_redirects=False) as client:
            tasks = [asyncio.create_task(self._source_loop(index, client)) for index in range(len(self.sources))]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def configured_observer(manager):
    config_path = os.environ.get(CONFIG_ENV)
    if config_path:
        try:
            with Path(config_path).open("rb") as stream:
                content = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Oversized observation configuration")
            return IdleObserver(ObservationConfig.model_validate_json(content), manager)
        except (OSError, ValueError):
            logger.error("Low-utilization observation disabled by an unreadable or invalid configuration")
    return None

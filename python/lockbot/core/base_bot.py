r"""
lockbot - BaseLockBot
"""

import copy
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version

from lockbot.core.config import Config
from lockbot.core.i18n import t
from lockbot.core.platforms.registry import create_adapter
from lockbot.core.utils import format_duration


def _get_version():
    try:
        return _pkg_version("lockbot")
    except Exception:
        return "unknown"


class AcquisitionLimitExceeded(ValueError):
    """A request would increase this identity's holdings beyond its caller policy."""


class BotState:
    """Manages bot state by delegating to a loader function."""

    _loader = None  # Subclasses or instances must set this

    def __init__(self, config=None):
        result = self._loader(config=config)
        if isinstance(result, tuple):
            self.bot_state, self.clamped_user_ids = result
        else:
            self.bot_state = result
            self.clamped_user_ids = set()


class BaseLockBot:
    """
    Base class for all lock bots.  Provides common infrastructure:
    - construction (config / state / lock / adapter)
    - show_error
    - timer_routine
    - print_help (template-method: header + _help_commands + footer)
    - _msg_with_usage  convenience helper
    - _build_alert_header
    """

    # Subclasses MUST define an inner _state_class(BotState) with a _loader.
    _state_class = None

    logger = logging.getLogger("lockbot.timer")

    # ------------------------------------------------------------------ init
    def __init__(self, config_dict=None, *, config=None, state=None, lock=None, adapter=None):
        """
        Create an isolated bot instance.

        Two usage patterns:
        - config_dict: pass a config dict, auto-creates config/state/lock
        - config/state/lock: inject existing objects directly (for testing)
        """
        if config is not None:
            self.config = config
        else:
            self.config = Config(config_dict or {})

        if state is not None:
            self.state = state
        else:
            self.state = self._state_class(config=self.config)

        self._lock = lock or threading.Lock()
        # The platform is read from config so that an injected adapter stays authoritative
        # and a bot whose stored platform is unknown fails at construction rather than at
        # the first message.
        self.adapter = adapter or create_adapter(self.config.get_val("PLATFORM", None), config=self.config)
        # Optional callback: invoked after a successful lock/slock so the
        # scheduler can recalculate its next wakeup without waiting for idle.
        self._on_state_changed: Callable[[], None] | None = None
        # Optional hook supplying identity-binding commands (bind / unbind / bindings).
        # core/ owns no database, so the backend injects this. None means the commands
        # report themselves as unavailable.
        self.binding_hook: Callable[..., str] | None = None

        self._notify_clamped_users()

    def _notify_clamped_users(self):
        """Send notification to users whose locks were shortened by max_duration reduction."""
        clamped = getattr(self.state, "clamped_user_ids", set())
        if not clamped:
            return
        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        dur_str = format_duration(max_dur, config=self.config)
        msg = t("notify.duration_clamped", config=self.config, max_duration=dur_str)
        reply = self.adapter.build_reply(msg, list(clamped))
        try:
            self.adapter.send(reply)
        except Exception:
            self.logger.warning("Failed to notify clamped users: %s", clamped)

    def _notify_state_changed(self) -> None:
        """Call _on_state_changed if wired up (no-op otherwise)."""
        if self._on_state_changed is not None:
            self._on_state_changed()

    def _save_and_notify(self) -> None:
        """Persist bot state to disk and wake the scheduler (if wired).

        Use this in every command handler that mutates state so it's
        impossible to forget either step.  The scheduler's
        ``_check_and_notify`` loop should still call ``save_bot_state_to_file``
        directly to avoid an unwanted reschedule from the timer thread.
        """
        from lockbot.core.io import save_bot_state_to_file

        save_bot_state_to_file(self.state.bot_state, config=self.config)
        self._notify_state_changed()

    def _check_acquisition_limit_locked(self, user_id, node_keys, limit):
        """Check projected node holdings while the caller owns ``_lock``.

        The limit is per invocation, supplied by a trusted transport. Chat keeps its
        existing policy. Devices on the same node count once; bookings count too.
        Renewals and claiming an existing booking never increase the held set.
        """
        if limit is None:
            return
        held = set()
        for key, node in self.state.bot_state.items():
            entries = [node] if isinstance(node, dict) else node
            for entry in entries:
                users = list(entry.get("current_users", [])) + list(entry.get("booking_list", []))
                if any(u["user_id"] == user_id for u in users):
                    held.add(key)
                    break
        additions = set(node_keys) - held
        if additions and len(held | additions) > limit:
            raise AcquisitionLimitExceeded(limit)

    def _resource_policy_locked(self, user_id, targets, *, claim_duration=None):
        """Apply bot policy before any mutation; caller holds the state lock."""
        from lockbot.core.resource_policy import policy_for_state, resource_entries

        if not self.config.get_val("FORBID_RELOCK") and self.config.get_val("MAX_RESOURCES_PER_USER", -1) == -1:
            return None
        entries = resource_entries(self.state.bot_state)
        policy = policy_for_state(self.state.bot_state, self.config, user_id)
        targets = set(targets)
        held = set(policy.pop("_claimed_resources"))
        cap = policy["max_resources_per_user"]
        if cap != -1 and targets - held and len(held | targets) > cap:
            return self.show_error(user_id, t("error.resource_count_limit", config=self.config, limit=cap))
        if policy["forbid_relock"]:
            for key in targets:
                entry = entries[key]
                if any(u["user_id"] == user_id for u in entry.get("current_users", [])):
                    return self.show_error(user_id, t("error.relock_forbidden", config=self.config))
                if claim_duration is not None:
                    booked = next((u for u in entry.get("booking_list", []) if u["user_id"] == user_id), None)
                    if booked and claim_duration > booked["duration"]:
                        return self.show_error(user_id, t("error.claim_duration_limit", config=self.config))
        return None

    # ------------------------------------------------------- state_snapshot
    def state_snapshot(self) -> tuple[datetime, dict]:
        """Return ``(captured_at, detached_copy_of_state)``, read under the state lock.

        Why this exists: every reader that wants to derive values from bot state needs the
        whole state as of one instant. Reading the live dict without the lock can observe a
        multi-resource mutation half-applied, and deriving "remaining time" fields from
        separate ``now()`` calls can produce a payload that contradicts itself.

        Two rules for callers:

        - The returned dict is a deep copy, so build the entire response from it and never
          touch ``self.state.bot_state`` afterwards.
        - ``self._lock`` is **not reentrant**. Do not call this while already holding it,
          and do not implement it by delegating to ``query()`` or any other command method,
          because those take the same lock and would self-deadlock.

        The critical section here is deliberately tiny: timestamp plus deep copy, no I/O.
        """
        with self._lock:
            return datetime.now(timezone.utc), copy.deepcopy(self.state.bot_state)

    # ---------------------------------------------------------- do_opkick
    def do_opkick(self, target_user, node_key=None, dev_ids=None, reason=""):
        """API-driven operator kick. Returns {"ok": True, "freed": [...]} or raises ValueError."""
        raise NotImplementedError("Subclass must implement do_opkick")

    # ---------------------------------------------------------- show_error
    def show_error(self, user_id, error_msg):
        """
        Show error message
        """
        return self.adapter.build_reply("\u274c" + error_msg.rstrip() + "\n\n", [user_id])

    @property
    def _duration_whitelist(self):
        return self.config.get_val("DURATION_WHITELIST") or []

    def _display_node(self, node_key):
        """Return node_key with alias suffix if SHOW_NODE_ALIAS is enabled."""
        if not self.config.get_val("SHOW_NODE_ALIAS"):
            return node_key
        aliases = self.config.get_val("NODE_ALIASES") or {}
        alias = aliases.get(node_key)
        return f"{node_key}「{alias}」" if alias else node_key

    def alias(self, user_id, command):
        """Set or clear a node alias. Usage: alias <node_key> [alias_value]"""
        if not self.config.get_val("ALLOW_USER_ALIAS"):
            return self.show_error(user_id, t("alias.disabled", config=self.config))

        import re

        m = re.match(r"^\s*alias\s+([\w\d_-]+)(?:\s+([\s\S]+?))?\s*$", command)
        if not m:
            return self.show_error(user_id, t("alias.usage", config=self.config))

        node_key = m.group(1)
        raw_alias_val = m.group(2) or ""
        if "\n" in raw_alias_val or "\r" in raw_alias_val:
            return self.show_error(user_id, t("alias.invalid_alias", config=self.config))
        alias_val = raw_alias_val.strip()

        if len(alias_val) > 15:
            return self.show_error(user_id, t("alias.too_long", config=self.config))

        cluster_configs = self.config.get_val("CLUSTER_CONFIGS")
        if node_key not in cluster_configs:
            return self.show_error(
                user_id,
                t(
                    "alias.invalid_node",
                    config=self.config,
                    node_key=node_key,
                    valid_keys=str(list(cluster_configs.keys())),
                ),
            )

        aliases = self.config.get_val("NODE_ALIASES") or {}
        if alias_val:
            aliases[node_key] = alias_val
        else:
            aliases.pop(node_key, None)
        self.config.set_val("NODE_ALIASES", aliases)

        # Persist to DB if running under platform mode
        self._persist_config_override("NODE_ALIASES", aliases)

        if alias_val:
            self.logger.info("user=%s set alias %s -> %s", user_id, node_key, alias_val)
            msg = t("alias.set_ok", config=self.config, node_key=node_key, alias=alias_val)
        else:
            self.logger.info("user=%s cleared alias %s", user_id, node_key)
            msg = t("alias.clear_ok", config=self.config, node_key=node_key)
        return self.adapter.build_reply(msg, [user_id])

    def _persist_config_override(self, key, value):
        """Persist a single config_override key to DB (best-effort)."""
        try:
            import json

            from lockbot.backend.app.bots.models import Bot
            from lockbot.backend.app.database import SessionLocal

            bot_id = self.config.get_val("BOT_ID")
            if not bot_id:
                return
            db = SessionLocal()
            try:
                bot = db.query(Bot).filter(Bot.id == int(bot_id)).first()
                if bot:
                    overrides = json.loads(bot.config_overrides) if bot.config_overrides else {}
                    overrides[key] = value
                    bot.config_overrides = json.dumps(overrides, ensure_ascii=False)
                    db.commit()
            finally:
                db.close()
        except Exception:
            pass

    def _whitelist_hint(self):
        """Return whitelist hint string if BOT_OWNER is configured, else empty string."""
        owner = self.config.get_val("BOT_OWNER")
        if not owner:
            return ""
        return t("error.whitelist_hint", config=self.config, owner=owner)

    # ------------------------------------------------------ _msg_with_usage
    def _msg_with_usage(self, msg_key, *, node_key=None, sep="", **kwargs):
        """Build usage text, or message parts when a successful request includes a link."""
        message = t(msg_key, config=self.config, **kwargs)
        usage = self._current_usage(node_key)
        if msg_key == "success.resource_locked":
            # Separate the title, resource list and user mention with one blank line.
            content = message.rstrip() + "\n\n" + usage.strip()
            if self._get_site_value("agent_promotion_enabled") == "true":
                skill_url = self._agent_skill_url()
                if skill_url:
                    return [content + "\n\n", (t("help.agent_lock_hint", config=self.config, url=""), skill_url), "\n"]
            return content + "\n\n"
        return message + sep + usage

    # ------------------------------------------------- _build_alert_header
    def _build_alert_header(self):
        """Build the common alert header used by ``_check_and_notify``."""
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")
        TIME_ALERT = self.config.get_val("TIME_ALERT")

        if EARLY_NOTIFY:
            alert_info = t(
                "alert.early_time_remaining",
                config=self.config,
                time_alert=format_duration(TIME_ALERT, config=self.config),
            )
            alert_info += t(
                "alert.early_no_renewal" if self.config.get_val("FORBID_RELOCK") else "alert.early_extend_reminder",
                config=self.config,
            )
            alert_info += t("alert.early_resource_list_header", config=self.config)
        else:
            alert_info = t("alert.auto_released_title", config=self.config)
            alert_info += t("alert.auto_released_list_header", config=self.config)
        return alert_info

    # --------------------------------------------------------- _help_header
    def _help_header(self):
        """Return the header section of the help text.  Override in subclasses."""
        EARLY_NOTIFY = self.config.get_val("EARLY_NOTIFY")

        parts = []
        parts.append(t("help.title", config=self.config))
        parts.append(t("help.section1_title", config=self.config))
        parts.append(
            t(
                "help.rule1_no_renewal" if self.config.get_val("FORBID_RELOCK") else "help.rule1_default_duration",
                config=self.config,
                default_duration=format_duration(self.config.get_val("DEFAULT_DURATION"), config=self.config),
            )
        )
        if EARLY_NOTIFY:
            parts.append(
                t(
                    "help.rule2_early_notification",
                    config=self.config,
                    time_alert=format_duration(self.config.get_val("TIME_ALERT"), config=self.config),
                )
            )
        else:
            parts.append(t("help.rule2_post_expiry_notification", config=self.config))
        return "".join(parts)

    # ---------------------------------------------------------- print_help
    def print_help(self, user_id, extra_info=None):
        """
        Show help message.  Uses the *template method* pattern:
        header + ``_help_commands()`` + footer.
        """
        reply_info = extra_info + "\n\n" if extra_info else ""
        # ---- header ----
        reply_info += self._help_header()

        # ---- commands (subclass hook) ----
        commands = self._help_commands()
        reply_info += commands
        skill_url = ""
        if extra_info is None:
            skill_url = self._agent_skill_url()
            if skill_url:
                number = max((int(n) for n in re.findall(r"(?m)^(\d+)\.", commands)), default=0) + 1
                reply_info = (
                    reply_info.rstrip() + "\n" + t("help.agent_commands", config=self.config, number=number) + "\n"
                )

        # ---- footer ----
        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        if max_dur > 0:
            reply_info += t(
                "help.max_duration_warning",
                config=self.config,
                max_duration=format_duration(max_dur, config=self.config),
            )

        cap = self.config.get_val("MAX_RESOURCES_PER_USER", -1)
        if cap != -1:
            reply_info += t("error.resource_count_limit", config=self.config, limit=cap) + "\n\n"

        # Compact footer line
        footer_parts = [f"v{_get_version()}"]
        bot_id = self.config.get_val("BOT_ID")
        if bot_id:
            footer_parts.append(f"ID: {bot_id}")
        bot_owner = self.config.get_val("BOT_OWNER")
        if bot_owner:
            footer_parts.append(t("help.bot_owner", config=self.config, owner=bot_owner).strip())
        reply_info += " | ".join(footer_parts) + "\n"

        # ---- news (only on explicit help) ----
        if extra_info is None:
            news = self._get_news_content()
            if news:
                reply_info += "\n"
                reply_info += t("help.news_header", config=self.config)
                reply_info += news + "\n"

        # ---- project links (only on explicit help) ----
        help_links = []
        if extra_info is None:
            platform_url = self._get_site_value("platform_url") or self.config.get_val("PLATFORM_URL")
            github_url = self._get_site_value("github_url") or self.config.get_val("GITHUB_URL")
            if platform_url or github_url or skill_url:
                help_links.append("\n")
            if platform_url:
                help_links.append((t("help.platform_url", config=self.config), platform_url))
            if github_url:
                help_links.append((t("help.github_url", config=self.config), github_url))
            if skill_url:
                help_links.append((t("help.agent_skill", config=self.config), skill_url))

        if help_links:
            reply_info = [reply_info] + help_links
        # Ensure a blank line before @mention
        if isinstance(reply_info, list):
            reply_info.append("\n")
        else:
            reply_info += "\n"
        return self.adapter.build_reply(reply_info, [user_id])

    def _agent_skill_url(self):
        getter = self.config.get_val("_AGENT_SKILL_URL")
        if not callable(getter):
            return ""
        try:
            return getter() or ""
        except Exception:
            return ""

    def _query_reply(self, content, user_id):
        """Append the current read-only page outside the resource-state lock."""
        getter = self.config.get_val("_BOT_VIEW_LINK")
        if callable(getter):
            try:
                view = getter()
                if view.get("view_url"):
                    content = [content.rstrip() + "\n\n", (t("view.link", config=self.config), view["view_url"]), "\n"]
            except Exception:
                self.logger.warning("Could not obtain resource view link for bot %s", self.config.get_val("BOT_ID"))
        return self.adapter.build_reply(content, [user_id])

    def _help_commands(self):
        """Return the command-section of the help text.  Override in subclasses."""
        return ""

    _site_cache = {}
    _site_cache_ts = 0.0
    _SITE_CACHE_TTL = 6 * 3600  # 6 hours

    @classmethod
    def _invalidate_site_cache(cls):
        """Force next _get_site_value call to read from DB."""
        BaseLockBot._site_cache = {}
        BaseLockBot._site_cache_ts = 0.0

    @classmethod
    def _get_site_value(cls, key: str) -> str:
        """Read a site setting from DB with TTL cache."""
        import time

        # Site settings are shared by NODE, DEVICE and QUEUE bots.
        cls = BaseLockBot
        now = time.time()
        if now - cls._site_cache_ts > cls._SITE_CACHE_TTL:
            cls._site_cache = {}
            cls._site_cache_ts = now
            try:
                from lockbot.backend.app.database import SessionLocal
                from lockbot.backend.app.settings.models import SiteSetting

                db = SessionLocal()
                try:
                    for row in db.query(SiteSetting).all():
                        cls._site_cache[row.key] = row.value.strip() if row.value else ""
                finally:
                    db.close()
            except Exception:
                pass
        return cls._site_cache.get(key, "")

    def _get_news_content(self) -> str:
        """Read news_content from site_settings (max 200 chars)."""
        text = self._get_site_value("news_content")
        if len(text) > 30:
            text = text[:30] + "..."
        return text

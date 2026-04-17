r"""
lockbot - BaseLockBot
"""

import logging
import threading
from collections.abc import Callable
from importlib.metadata import version as _pkg_version

from lockbot.core.config import Config
from lockbot.core.i18n import t
from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.utils import format_duration


def _get_version():
    try:
        return _pkg_version("lockbot")
    except Exception:
        return "unknown"


class BotState:
    """Manages bot state by delegating to a loader function."""

    _loader = None  # Subclasses or instances must set this

    def __init__(self, config=None):
        self.bot_state = self._loader(config=config)


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
        self.adapter = adapter or InfoflowAdapter(config=self.config)
        # Optional callback: invoked after a successful lock/slock so the
        # scheduler can recalculate its next wakeup without waiting for idle.
        self._on_state_changed: Callable[[], None] | None = None

    def _notify_state_changed(self) -> None:
        """Call _on_state_changed if wired up (no-op otherwise)."""
        if self._on_state_changed is not None:
            self._on_state_changed()

    # ---------------------------------------------------------- show_error
    def show_error(self, user_id, error_msg):
        """
        Show error message
        """
        return self.adapter.build_reply("\u274c" + error_msg, [user_id])

    # ------------------------------------------------------ _msg_with_usage
    def _msg_with_usage(self, msg_key, *, node_key=None, sep="", **kwargs):
        """Return ``t(msg_key, ...) + sep + self._current_usage(node_key)``."""
        return t(msg_key, config=self.config, **kwargs) + sep + self._current_usage(node_key)

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
            alert_info += t("alert.early_extend_reminder", config=self.config)
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
                "help.rule1_default_duration",
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
        reply_info += self._help_commands()

        # ---- footer ----
        max_dur = self.config.get_val("MAX_LOCK_DURATION")
        if max_dur > 0:
            reply_info += t(
                "help.max_duration_warning",
                config=self.config,
                max_duration=format_duration(max_dur, config=self.config),
            )

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
            if platform_url or github_url:
                help_links.append("\n")
            if platform_url:
                help_links.append((t("help.platform_url", config=self.config), platform_url))
            if github_url:
                help_links.append((t("help.github_url", config=self.config), github_url))

        if help_links:
            reply_info = [reply_info] + help_links
        # Ensure a blank line before @mention
        if isinstance(reply_info, list):
            reply_info.append("\n")
        else:
            reply_info += "\n"
        return self.adapter.build_reply(reply_info, [user_id])

    def _help_commands(self):
        """Return the command-section of the help text.  Override in subclasses."""
        return ""

    _site_cache = {}
    _site_cache_ts = 0.0
    _SITE_CACHE_TTL = 6 * 3600  # 6 hours

    @classmethod
    def _invalidate_site_cache(cls):
        """Force next _get_site_value call to read from DB."""
        cls._site_cache = {}
        cls._site_cache_ts = 0.0

    @classmethod
    def _get_site_value(cls, key: str) -> str:
        """Read a site setting from DB with TTL cache."""
        import time

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

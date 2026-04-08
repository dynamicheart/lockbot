"""
Internationalization module for lockbot.

Usage:
    from lockbot.core.i18n import t

    # With a config instance (per-bot language):
    t("success.resource_locked", config=self.config)

    # With explicit language:
    t("duration.days", lang="en", value="2.0")

    # Default (Chinese):
    t("status.idle")
"""

from lockbot.core.i18n.en import MESSAGES as _EN
from lockbot.core.i18n.zh import MESSAGES as _ZH

_LANGS = {
    "zh": _ZH,
    "en": _EN,
}

DEFAULT_LANG = "zh"


def t(key: str, *, lang: str = None, config=None, **kwargs) -> str:
    """Look up a translated string by key.

    Args:
        key: Message key (e.g. "success.resource_locked").
        lang: Language code ("zh" or "en"). Overrides config.
        config: Config instance; reads LANGUAGE field if lang is not set.
        **kwargs: Format arguments for the message template.

    Returns:
        Formatted message string. Falls back to zh, then returns the key itself.
    """
    if lang is None:
        if config is not None:
            lang = config.get_val("LANGUAGE", DEFAULT_LANG)
        else:
            lang = DEFAULT_LANG

    messages = _LANGS.get(lang, _ZH)
    template = messages.get(key)
    if template is None:
        # Fallback to Chinese
        template = _ZH.get(key)
    if template is None:
        return key

    if kwargs:
        return template.format(**kwargs)
    return template

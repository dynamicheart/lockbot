"""
Platform adapter registry and factory.

Bots store a platform value (e.g. ``"Infoflow"``) alongside their credentials.  This
module turns that stored value into a :class:`~lockbot.core.message_adapter.MessageAdapter`
instance, so that no layer above the adapter needs to know which IM platform is in use.

Rules:

- Platform names are normalized to lowercase for lookup; ``"Infoflow"``, ``"infoflow"``
  and ``" InfoFlow "`` all resolve to ``infoflow``.
- A missing / empty platform value resolves to :data:`DEFAULT_PLATFORM`, which is what
  existing rows (written before the value was read) mean.
- An unknown platform value raises :class:`UnknownPlatformError` rather than silently
  falling back to Infoflow, so a typo or an unsupported platform fails loudly.
"""

from lockbot.core.message_adapter import MessageAdapter

#: Platform assumed for bots that never recorded one.
DEFAULT_PLATFORM = "infoflow"

#: Lazily imported built-in adapters, keyed by normalized platform name.
_BUILTIN_ADAPTERS = {
    DEFAULT_PLATFORM: ("lockbot.core.platforms.infoflow", "InfoflowAdapter"),
    "feishu": ("lockbot.core.platforms.feishu", "FeishuAdapter"),
}

_REGISTRY: dict[str, type[MessageAdapter]] = {}


class UnknownPlatformError(ValueError):
    """Raised when a platform value does not correspond to any registered adapter."""

    def __init__(self, platform):
        self.platform = platform
        super().__init__(f"Unknown platform {platform!r}; registered platforms: {sorted(available_platforms())}")


def register_adapter(adapter_cls: type[MessageAdapter]) -> type[MessageAdapter]:
    """Register an adapter class under its own ``platform_name``.

    Returns the class, so it can be used as a decorator.
    """
    name = getattr(adapter_cls, "platform_name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{adapter_cls.__name__} must define a non-empty 'platform_name'")
    if name != name.strip().lower():
        raise ValueError(f"{adapter_cls.__name__}.platform_name must be lowercase and unpadded, got {name!r}")
    _REGISTRY[name] = adapter_cls
    return adapter_cls


def unregister_adapter(platform: str) -> None:
    """Remove an adapter registration. Intended for test cleanup."""
    _REGISTRY.pop(platform, None)


def _load_builtin(name: str) -> type[MessageAdapter] | None:
    """Import and register a built-in adapter on first use."""
    target = _BUILTIN_ADAPTERS.get(name)
    if target is None:
        return None
    module_path, class_name = target
    import importlib

    adapter_cls = getattr(importlib.import_module(module_path), class_name)
    return register_adapter(adapter_cls)


def available_platforms() -> set[str]:
    """Return every platform name that can be resolved, built-in or registered."""
    return set(_REGISTRY) | set(_BUILTIN_ADAPTERS)


def normalize_platform_name(platform=None) -> str:
    """Normalize a stored platform value to a registered platform name.

    ``None``, a non-string, and a blank string all mean "not recorded" and resolve to
    :data:`DEFAULT_PLATFORM`.  Any other unrecognized value raises
    :class:`UnknownPlatformError`.
    """
    if platform is None or not isinstance(platform, str) or not platform.strip():
        return DEFAULT_PLATFORM
    name = platform.strip().lower()
    if name not in available_platforms():
        raise UnknownPlatformError(platform)
    return name


def get_adapter_class(platform=None) -> type[MessageAdapter]:
    """Resolve the adapter class for a stored platform value."""
    name = normalize_platform_name(platform)
    adapter_cls = _REGISTRY.get(name) or _load_builtin(name)
    if adapter_cls is None:
        raise UnknownPlatformError(platform)
    return adapter_cls


def create_adapter(platform=None, *, config=None) -> MessageAdapter:
    """Instantiate the adapter for a stored platform value.

    Args:
        platform: The bot's stored platform value, or None for the default.
        config: Config instance passed to the adapter for platform credentials.
    """
    return get_adapter_class(platform)(config=config)

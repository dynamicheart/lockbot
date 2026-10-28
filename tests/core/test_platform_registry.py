"""AC-8: platform resolution must be normalized, pluggable, and loud about the unknown."""

import pytest
from fake_adapter import FakeAdapter
from lockbot.core.bot_instance import BotInstance
from lockbot.core.message_adapter import MessageAdapter
from lockbot.core.node_bot import NodeBot
from lockbot.core.platforms.infoflow import InfoflowAdapter
from lockbot.core.platforms.registry import (
    DEFAULT_PLATFORM,
    UnknownPlatformError,
    create_adapter,
    get_adapter_class,
    normalize_platform_name,
    register_adapter,
    unregister_adapter,
)


@pytest.fixture
def fake_platform():
    """Register FakeAdapter for the duration of a test, then remove it."""
    register_adapter(FakeAdapter)
    yield FakeAdapter.platform_name
    unregister_adapter(FakeAdapter.platform_name)


class TestNormalization:
    @pytest.mark.parametrize("stored", ["Infoflow", "infoflow", "INFOFLOW", "  InfoFlow  "])
    def test_existing_spellings_normalize(self, stored):
        assert normalize_platform_name(stored) == "infoflow"

    @pytest.mark.parametrize("stored", [None, "", "   "])
    def test_unrecorded_platform_falls_back_to_default(self, stored):
        """Rows written before the platform value was read mean 'infoflow'."""
        assert normalize_platform_name(stored) == DEFAULT_PLATFORM

    @pytest.mark.parametrize("stored", ["feishu", "slack", "infoflow2", "如流"])
    def test_unknown_platform_raises_instead_of_falling_back(self, stored):
        with pytest.raises(UnknownPlatformError) as exc:
            normalize_platform_name(stored)
        assert exc.value.platform == stored


class TestAdapterResolution:
    def test_default_adapter_is_infoflow_and_reports_its_name(self):
        adapter = create_adapter()
        assert isinstance(adapter, InfoflowAdapter)
        assert adapter.platform_name == "infoflow"

    def test_adapters_expose_a_normalized_platform_name(self):
        for adapter_cls in (InfoflowAdapter, FakeAdapter):
            name = adapter_cls.platform_name
            assert name and name == name.strip().lower()

    def test_stored_value_resolves_to_adapter_class(self):
        assert get_adapter_class("Infoflow") is InfoflowAdapter

    def test_config_is_passed_through(self):
        sentinel = object()
        assert create_adapter("Infoflow", config=sentinel).config is sentinel

    def test_unknown_platform_raises_on_create(self):
        with pytest.raises(UnknownPlatformError):
            create_adapter("feishu")

    def test_registered_platform_resolves_without_touching_infoflow(self, fake_platform):
        adapter = create_adapter(fake_platform.upper())
        assert isinstance(adapter, FakeAdapter)
        assert not isinstance(adapter, InfoflowAdapter)

    def test_unregistering_restores_the_unknown_error(self):
        register_adapter(FakeAdapter)
        unregister_adapter(FakeAdapter.platform_name)
        with pytest.raises(UnknownPlatformError):
            create_adapter(FakeAdapter.platform_name)


class TestRegistrationValidation:
    def test_adapter_without_platform_name_is_rejected(self):
        class Nameless(MessageAdapter):
            def verify_request(self, signature, **kwargs):
                return False

            def decrypt_payload(self, encrypted_data, **kwargs):
                return None

            def extract_command(self, msg_data):
                return "", "", ""

            def build_reply(self, content, user_ids, group_id=None):
                return {}

            def send(self, msg):
                return []

        with pytest.raises(ValueError):
            register_adapter(Nameless)

    def test_non_normalized_platform_name_is_rejected(self):
        class Shouty(FakeAdapter):
            platform_name = "FakeIM"

        with pytest.raises(ValueError):
            register_adapter(Shouty)


class TestBotAdapterSelection:
    def test_bot_without_platform_config_gets_the_default_adapter(self):
        instance = BotInstance("NODE", {"BOT_NAME": "p1", "CLUSTER_CONFIGS": {"n1": "m1"}}, auto_start=False)
        assert isinstance(instance.bot.adapter, InfoflowAdapter)

    def test_bot_platform_config_selects_the_adapter(self, fake_platform):
        instance = BotInstance(
            "NODE",
            {"BOT_NAME": "p2", "CLUSTER_CONFIGS": {"n1": "m1"}, "PLATFORM": fake_platform},
            auto_start=False,
        )
        assert isinstance(instance.bot.adapter, FakeAdapter)

    def test_bot_with_unknown_platform_fails_at_construction(self):
        with pytest.raises(UnknownPlatformError):
            BotInstance(
                "NODE",
                {"BOT_NAME": "p3", "CLUSTER_CONFIGS": {"n1": "m1"}, "PLATFORM": "feishu"},
                auto_start=False,
            )

    def test_injected_adapter_wins_and_skips_platform_resolution(self):
        """An explicitly injected adapter is authoritative: no lookup happens at all."""
        injected = FakeAdapter()
        bot = NodeBot(
            config_dict={"BOT_NAME": "p4", "CLUSTER_CONFIGS": {"n1": "m1"}, "PLATFORM": "feishu"},
            adapter=injected,
        )
        assert bot.adapter is injected

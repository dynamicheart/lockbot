from lockbot.core.node_bot import NodeBot


def test_help_links_to_skill_without_promoting_it_on_success(tmp_path):
    enabled = [True]
    url = "https://lockbot.example/api/agent-skill/SKILL.md"
    bot = NodeBot(
        config_dict={
            "BOT_ID": "help-test",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["n1"],
            "_AGENT_SKILL_URL": lambda: url if enabled[0] else "",
        }
    )
    help_reply = bot.print_help("alice")
    text = bot.adapter.extract_reply_text(help_reply)
    assert "bind <" in text and "bindings" in text and "unbind" in text
    assert any(item.get("href") == url for item in help_reply["message"]["body"])
    result = bot.lock("alice", "lock n1 1h")
    assert url not in bot.adapter.extract_reply_text(result)
    assert url not in bot.adapter.extract_reply_text(bot.lock("bob", "lock n1 1h"))
    enabled[0] = False
    disabled = bot.adapter.extract_reply_text(bot.print_help("alice"))
    assert url not in disabled
    assert "bind <" not in disabled and "bindings" not in disabled and "unbind" not in disabled
    enabled[0] = True
    assert "bindings" in bot.adapter.extract_reply_text(bot.print_help("alice"))


def test_agent_section_and_footer_link_order(tmp_path):
    from lockbot.core.queue_bot import QueueBot

    bot = QueueBot(
        config_dict={
            "BOT_ID": 4,
            "BOT_OWNER": "admin",
            "PLATFORM_URL": "https://example.test",
            "GITHUB_URL": "https://github.com/example/lockbot",
            "DATA_DIR": str(tmp_path),
            "CLUSTER_CONFIGS": ["n1"],
            "_AGENT_SKILL_URL": lambda: "https://example.test/skill",
        }
    )
    reply = bot.print_help("alice")
    text = bot.adapter.extract_reply_text(reply)
    assert text.index("8. Agent:") < text.index("ID: 4") < text.index("GitHub:") < text.index("Agent 使用：")
    assert "n1\n\n8. Agent" not in text
    assert "Agent 自动申请资源：" not in text

"""Tests for Infoflow adapter build_reply with mixed content."""

from lockbot.core.platforms.infoflow import InfoflowAdapter


class TestInfoflowBuildReply:
    def test_basic_str_text_and_at(self):
        adapter = InfoflowAdapter()
        msg = adapter.build_reply("hello", ["user1"])
        body = msg["message"]["body"]
        types = [b["type"] for b in body]
        assert types == ["TEXT", "AT"]
        assert body[0]["content"] == "hello"
        assert body[1]["atuserids"] == ["user1"]

    def test_list_with_text_items(self):
        adapter = InfoflowAdapter()
        msg = adapter.build_reply(["line1\n", "line2\n"], ["user1"])
        body = msg["message"]["body"]
        types = [b["type"] for b in body]
        assert types == ["TEXT", "TEXT", "AT"]

    def test_list_with_link_tuple(self):
        adapter = InfoflowAdapter()
        msg = adapter.build_reply(["see link\n", ("label: ", "https://example.com")], ["user1"])
        body = msg["message"]["body"]
        types = [b["type"] for b in body]
        assert types == ["TEXT", "TEXT", "LINK", "TEXT", "AT"]
        assert body[1]["content"] == "label: "
        assert body[2]["href"] == "https://example.com"
        assert body[3]["content"] == "\n"

    def test_multiple_links_interleaved(self):
        adapter = InfoflowAdapter()
        items = [
            "text\n",
            "\n",
            ("platform:\n", "http://1.2.3.4:8080"),
            ("github:\n", "https://github.com/dynamicheart/lockbot"),
        ]
        msg = adapter.build_reply(items, ["u1"])
        body = msg["message"]["body"]
        types = [b["type"] for b in body]
        assert types == ["TEXT", "TEXT", "TEXT", "LINK", "TEXT", "TEXT", "LINK", "TEXT", "AT"]
        assert body[3]["href"] == "http://1.2.3.4:8080"
        assert body[6]["href"] == "https://github.com/dynamicheart/lockbot"

    def test_with_group_id(self):
        adapter = InfoflowAdapter()
        msg = adapter.build_reply("hi", ["u1"], group_id="group123")
        assert msg["message"]["header"]["toid"] == "group123"

    def test_empty_list(self):
        adapter = InfoflowAdapter()
        msg = adapter.build_reply([], ["u1"])
        body = msg["message"]["body"]
        assert len(body) == 1
        assert body[0]["type"] == "AT"

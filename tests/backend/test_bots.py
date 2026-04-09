"""
Bot CRUD tests.
"""


def _sample_bot(name="mybot"):
    return {
        "name": name,
        "bot_type": "NODE",
        "webhook_url": "https://example.com/webhook",
        "aes_key": "testaeskey",
        "token": "testtoken",
        "cluster_configs": ["node1", "node2"],
    }


class TestCreateBot:
    def test_create_success(self, client, auth_header):
        resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "mybot"
        assert data["bot_type"] == "NODE"
        assert data["status"] == "stopped"

    def test_create_device_bot(self, client, auth_header):
        resp = client.post(
            "/api/bots",
            json={
                **_sample_bot("devbot"),
                "bot_type": "DEVICE",
                "cluster_configs": {"h1": ["A100", "A100", "A100", "A100"]},
            },
            headers=auth_header,
        )
        assert resp.status_code == 201
        assert resp.json()["bot_type"] == "DEVICE"

    def test_create_duplicate_name(self, client, auth_header):
        client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        assert resp.status_code == 409

    def test_create_invalid_type(self, client, auth_header):
        resp = client.post("/api/bots", json={**_sample_bot(), "bot_type": "INVALID"}, headers=auth_header)
        assert resp.status_code == 422

    def test_create_no_auth(self, client):
        resp = client.post("/api/bots", json=_sample_bot())
        assert resp.status_code == 403


class TestListBots:
    def test_list_empty(self, client, auth_header):
        resp = client.get("/api/bots", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_own_bots(self, client, auth_header):
        client.post("/api/bots", json=_sample_bot("bot1"), headers=auth_header)
        client.post("/api/bots", json=_sample_bot("bot2"), headers=auth_header)
        resp = client.get("/api/bots", headers=auth_header)
        assert len(resp.json()) == 2


class TestGetBot:
    def test_get_detail(self, client, auth_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        bot_id = create_resp.json()["id"]
        resp = client.get(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url_masked"].startswith("***")
        assert data["token_masked"].startswith("***")

    def test_get_not_found(self, client, auth_header):
        resp = client.get("/api/bots/99999", headers=auth_header)
        assert resp.status_code == 404


class TestUpdateBot:
    def test_update_name(self, client, auth_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        bot_id = create_resp.json()["id"]
        resp = client.put(f"/api/bots/{bot_id}", json={"name": "newname"}, headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["name"] == "newname"

    def test_update_config_overrides_merged_into_build_config(self, client, auth_header, db_session):
        """Verify config_overrides from update_bot are merged by _build_config_dict."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict

        create_resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        bot_id = create_resp.json()["id"]

        overrides = {"MAX_LOCK_DURATION": 1800, "EARLY_NOTIFY": True}
        resp = client.put(f"/api/bots/{bot_id}", json={"config_overrides": overrides}, headers=auth_header)
        assert resp.status_code == 200

        bot = db_session.get(Bot, bot_id)
        config_dict = _build_config_dict(bot)

        assert config_dict["BOT_ID"] == bot_id
        assert config_dict["CLUSTER_CONFIGS"] == ["node1", "node2"]
        assert config_dict["MAX_LOCK_DURATION"] == 1800
        assert config_dict["EARLY_NOTIFY"] is True

    def test_update_config_overrides_then_bot_initializes(self, client, auth_header, db_session, tmp_path):
        """Verify a bot can be initialized with config from _build_config_dict."""
        import os

        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.io import save_bot_state_to_file
        from lockbot.core.node_bot import NodeBot

        create_resp = client.post("/api/bots", json=_sample_bot("cfgbot"), headers=auth_header)
        bot_id = create_resp.json()["id"]

        overrides = {"MAX_LOCK_DURATION": 600, "DATA_DIR": str(tmp_path)}
        client.put(f"/api/bots/{bot_id}", json={"config_overrides": overrides}, headers=auth_header)

        bot_record = db_session.get(Bot, bot_id)
        config_dict = _build_config_dict(bot_record)

        node_bot = NodeBot(config_dict=config_dict)

        # Verify config override is applied
        assert node_bot.config.get_val("MAX_LOCK_DURATION") == 600
        # Trigger state save to verify DATA_DIR is used
        save_bot_state_to_file(node_bot.state.bot_state, config=node_bot.config)
        state_file = os.path.join(str(tmp_path), str(bot_id), "bot_state.json")
        assert os.path.exists(state_file)


class TestDeleteBot:
    def test_delete_success(self, client, auth_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=auth_header)
        bot_id = create_resp.json()["id"]
        resp = client.delete(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 204

        resp = client.get(f"/api/bots/{bot_id}", headers=auth_header)
        assert resp.status_code == 404

    def test_delete_not_found(self, client, auth_header):
        resp = client.delete("/api/bots/99999", headers=auth_header)
        assert resp.status_code == 404


def _build_device_bot(client, auth_header, db_session, tmp_path, name="devbot", configs=None):
    """Create a DEVICE bot with DATA_DIR in tmp_path, return (bot_id, config_dict)."""
    from lockbot.backend.app.bots.models import Bot
    from lockbot.backend.app.bots.router import _build_config_dict

    if configs is None:
        configs = {"h1": ["A100", "A100"], "h2": ["H100"]}
    create_resp = client.post(
        "/api/bots",
        json={
            "name": name,
            "bot_type": "DEVICE",
            "webhook_url": "https://example.com/hook",
            "aes_key": "testaeskey",
            "token": "testtoken",
            "cluster_configs": configs,
        },
        headers=auth_header,
    )
    assert create_resp.status_code == 201
    bot_id = create_resp.json()["id"]

    client.put(f"/api/bots/{bot_id}", json={"config_overrides": {"DATA_DIR": str(tmp_path)}}, headers=auth_header)

    bot_record = db_session.get(Bot, bot_id)
    config_dict = _build_config_dict(bot_record)
    return bot_id, config_dict


class TestClusterConfigChange:
    """Test adding/removing nodes and devices via update_bot → bot restart."""

    def test_add_device_to_node(self, client, auth_header, db_session, tmp_path):
        """Add a device to an existing node, verify state migrates correctly."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            auth_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"], "h2": ["H100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h1 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        # Update cluster_configs: add a 3rd A100 to h1
        new_configs = {"h1": ["A100", "A100", "A100"], "h2": ["H100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=auth_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert len(bot2.state.bot_state["h1"]) == 3
        assert bot2.state.bot_state["h1"][0]["status"] == "exclusive"
        assert bot2.state.bot_state["h1"][2]["status"] == "idle"

    def test_remove_node(self, client, auth_header, db_session, tmp_path):
        """Remove a node, verify it's dropped from state."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            auth_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"], "h2": ["H100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h2 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = {"h1": ["A100", "A100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=auth_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert "h1" in bot2.state.bot_state
        assert "h2" not in bot2.state.bot_state

    def test_add_new_node(self, client, auth_header, db_session, tmp_path):
        """Add a brand new node, verify it gets default state."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            auth_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h1 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = {"h1": ["A100", "A100"], "h2": ["H100", "H100", "H100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=auth_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert len(bot2.state.bot_state["h2"]) == 3
        assert all(d["status"] == "idle" for d in bot2.state.bot_state["h2"])
        assert bot2.state.bot_state["h1"][0]["status"] == "exclusive"

    def test_node_bot_add_node(self, client, auth_header, db_session, tmp_path):
        """Test adding a node to a NODE-type bot."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.io import save_bot_state_to_file
        from lockbot.core.node_bot import NodeBot

        create_resp = client.post(
            "/api/bots",
            json={
                "name": "nodebot",
                "bot_type": "NODE",
                "webhook_url": "https://example.com/hook",
                "aes_key": "testaeskey",
                "token": "testtoken",
                "cluster_configs": ["n1"],
            },
            headers=auth_header,
        )
        bot_id = create_resp.json()["id"]

        client.put(f"/api/bots/{bot_id}", json={"config_overrides": {"DATA_DIR": str(tmp_path)}}, headers=auth_header)

        bot_record = db_session.get(Bot, bot_id)
        config_dict = _build_config_dict(bot_record)

        bot = NodeBot(config_dict=config_dict)
        bot.lock("user1", "lock n1 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = ["n1", "n2"]
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=auth_header)

        db_session.expire_all()
        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = NodeBot(config_dict=new_config_dict)
        assert bot2.state.bot_state["n1"]["status"] == "exclusive"
        assert bot2.state.bot_state["n2"]["status"] == "idle"

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
    def test_create_success(self, client, admin_header):
        resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "mybot"
        assert data["bot_type"] == "NODE"
        assert data["status"] == "stopped"

    def test_create_device_bot(self, client, admin_header):
        resp = client.post(
            "/api/bots",
            json={
                **_sample_bot("devbot"),
                "bot_type": "DEVICE",
                "cluster_configs": {"h1": ["A100", "A100", "A100", "A100"]},
            },
            headers=admin_header,
        )
        assert resp.status_code == 201
        assert resp.json()["bot_type"] == "DEVICE"

    def test_create_duplicate_name(self, client, admin_header):
        client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        assert resp.status_code == 409

    def test_create_invalid_type(self, client, admin_header):
        resp = client.post("/api/bots", json={**_sample_bot(), "bot_type": "INVALID"}, headers=admin_header)
        assert resp.status_code == 422

    def test_create_no_auth(self, client):
        resp = client.post("/api/bots", json=_sample_bot())
        assert resp.status_code == 401


class TestListBots:
    def test_list_empty(self, client, admin_header):
        resp = client.get("/api/bots", headers=admin_header)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_own_bots(self, client, admin_header):
        client.post("/api/bots", json=_sample_bot("bot1"), headers=admin_header)
        client.post("/api/bots", json=_sample_bot("bot2"), headers=admin_header)
        resp = client.get("/api/bots", headers=admin_header)
        assert len(resp.json()) == 2


class TestGetBot:
    def test_get_detail(self, client, admin_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = create_resp.json()["id"]
        resp = client.get(f"/api/bots/{bot_id}", headers=admin_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url_masked"].startswith("***")
        assert data["token_masked"].startswith("***")

    def test_get_not_found(self, client, admin_header):
        resp = client.get("/api/bots/99999", headers=admin_header)
        assert resp.status_code == 404


class TestUpdateBot:
    def test_update_name(self, client, admin_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = create_resp.json()["id"]
        resp = client.put(f"/api/bots/{bot_id}", json={"name": "newname"}, headers=admin_header)
        assert resp.status_code == 200
        assert resp.json()["name"] == "newname"

    def test_update_config_overrides_merged_into_build_config(self, client, admin_header, db_session):
        """Verify config_overrides from update_bot are merged by _build_config_dict."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict

        create_resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = create_resp.json()["id"]

        overrides = {"MAX_LOCK_DURATION": 1800, "EARLY_NOTIFY": True}
        resp = client.put(f"/api/bots/{bot_id}", json={"config_overrides": overrides}, headers=admin_header)
        assert resp.status_code == 200

        bot = db_session.get(Bot, bot_id)
        config_dict = _build_config_dict(bot)

        assert config_dict["BOT_ID"] == bot_id
        assert config_dict["CLUSTER_CONFIGS"] == ["node1", "node2"]
        assert config_dict["MAX_LOCK_DURATION"] == 1800
        assert config_dict["EARLY_NOTIFY"] is True

    def test_update_config_overrides_then_bot_initializes(self, client, admin_header, db_session, tmp_path):
        """Verify a bot can be initialized with config from _build_config_dict."""
        import os

        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.io import save_bot_state_to_file
        from lockbot.core.node_bot import NodeBot

        create_resp = client.post("/api/bots", json=_sample_bot("cfgbot"), headers=admin_header)
        bot_id = create_resp.json()["id"]

        overrides = {"MAX_LOCK_DURATION": 600, "DATA_DIR": str(tmp_path)}
        client.put(f"/api/bots/{bot_id}", json={"config_overrides": overrides}, headers=admin_header)

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
    def test_delete_success(self, client, admin_header):
        create_resp = client.post("/api/bots", json=_sample_bot(), headers=admin_header)
        bot_id = create_resp.json()["id"]
        resp = client.delete(f"/api/bots/{bot_id}", headers=admin_header)
        assert resp.status_code == 204

        resp = client.get(f"/api/bots/{bot_id}", headers=admin_header)
        assert resp.status_code == 404

    def test_delete_not_found(self, client, admin_header):
        resp = client.delete("/api/bots/99999", headers=admin_header)
        assert resp.status_code == 404


def _build_device_bot(client, admin_header, db_session, tmp_path, name="devbot", configs=None):
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
        headers=admin_header,
    )
    assert create_resp.status_code == 201
    bot_id = create_resp.json()["id"]

    client.put(f"/api/bots/{bot_id}", json={"config_overrides": {"DATA_DIR": str(tmp_path)}}, headers=admin_header)

    bot_record = db_session.get(Bot, bot_id)
    config_dict = _build_config_dict(bot_record)
    return bot_id, config_dict


class TestClusterConfigChange:
    """Test adding/removing nodes and devices via update_bot → bot restart."""

    def test_add_device_to_node(self, client, admin_header, db_session, tmp_path):
        """Add a device to an existing node, verify state migrates correctly."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            admin_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"], "h2": ["H100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h1 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        # Update cluster_configs: add a 3rd A100 to h1
        new_configs = {"h1": ["A100", "A100", "A100"], "h2": ["H100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=admin_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert len(bot2.state.bot_state["h1"]) == 3
        assert bot2.state.bot_state["h1"][0]["status"] == "exclusive"
        assert bot2.state.bot_state["h1"][2]["status"] == "idle"

    def test_remove_node(self, client, admin_header, db_session, tmp_path):
        """Remove a node, verify it's dropped from state."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            admin_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"], "h2": ["H100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h2 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = {"h1": ["A100", "A100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=admin_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert "h1" in bot2.state.bot_state
        assert "h2" not in bot2.state.bot_state

    def test_add_new_node(self, client, admin_header, db_session, tmp_path):
        """Add a brand new node, verify it gets default state."""
        from lockbot.backend.app.bots.models import Bot
        from lockbot.backend.app.bots.router import _build_config_dict
        from lockbot.core.device_bot import DeviceBot
        from lockbot.core.io import save_bot_state_to_file

        bot_id, config_dict = _build_device_bot(
            client,
            admin_header,
            db_session,
            tmp_path,
            configs={"h1": ["A100", "A100"]},
        )

        bot = DeviceBot(config_dict=config_dict)
        bot.lock("user1", "lock h1 dev0 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = {"h1": ["A100", "A100"], "h2": ["H100", "H100", "H100"]}
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=admin_header)

        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = DeviceBot(config_dict=new_config_dict)
        assert len(bot2.state.bot_state["h2"]) == 3
        assert all(d["status"] == "idle" for d in bot2.state.bot_state["h2"])
        assert bot2.state.bot_state["h1"][0]["status"] == "exclusive"

    def test_node_bot_add_node(self, client, admin_header, db_session, tmp_path):
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
            headers=admin_header,
        )
        bot_id = create_resp.json()["id"]

        client.put(f"/api/bots/{bot_id}", json={"config_overrides": {"DATA_DIR": str(tmp_path)}}, headers=admin_header)

        bot_record = db_session.get(Bot, bot_id)
        config_dict = _build_config_dict(bot_record)

        bot = NodeBot(config_dict=config_dict)
        bot.lock("user1", "lock n1 1h")
        save_bot_state_to_file(bot.state.bot_state, config=bot.config)

        new_configs = ["n1", "n2"]
        client.put(f"/api/bots/{bot_id}", json={"cluster_configs": new_configs}, headers=admin_header)

        db_session.expire_all()
        bot_record = db_session.get(Bot, bot_id)
        new_config_dict = _build_config_dict(bot_record)

        bot2 = NodeBot(config_dict=new_config_dict)
        assert bot2.state.bot_state["n1"]["status"] == "exclusive"
        assert bot2.state.bot_state["n2"]["status"] == "idle"


class TestUpdateBotState:
    """Tests for PUT /bots/{id}/state validation and alignment."""

    @staticmethod
    def _create_bot(client, admin_header, bot_type="NODE", cluster_configs=None, tmp_path=None):
        resp = client.post(
            "/api/bots",
            json={
                "name": "statetest",
                "bot_type": bot_type,
                "webhook_url": "https://example.com/webhook",
                "aes_key": "testaeskey",
                "token": "testtoken",
                "cluster_configs": cluster_configs
                or (["n1", "n2"] if bot_type != "DEVICE" else {"n1": ["a100", "a100"]}),
            },
            headers=admin_header,
        )
        bot_id = resp.json()["id"]
        if tmp_path is not None:
            client.put(
                f"/api/bots/{bot_id}",
                json={"config_overrides": {"DATA_DIR": str(tmp_path)}},
                headers=admin_header,
            )
        return bot_id

    def test_update_valid_node_state(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        state = {
            "n1": {"status": "idle", "current_users": [], "booking_list": []},
            "n2": {
                "status": "exclusive",
                "current_users": [{"user_id": "u1", "start_time": 1000, "duration": 3600}],
                "booking_list": [],
            },
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert "warnings" not in resp.json() or resp.json()["warnings"] == []

    def test_update_extra_nodes_removed(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        state = {
            "n1": {"status": "idle", "current_users": [], "booking_list": []},
            "n2": {"status": "idle", "current_users": [], "booking_list": []},
            "n3": {"status": "idle", "current_users": [], "booking_list": []},
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("n3" in w for w in resp.json()["warnings"])

    def test_update_missing_nodes_added(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        state = {"n1": {"status": "idle", "current_users": [], "booking_list": []}}
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("n2" in w for w in resp.json()["warnings"])

    def test_update_invalid_status_fixed(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        state = {
            "n1": {"status": "busy", "current_users": [], "booking_list": []},
            "n2": {"status": "idle", "current_users": [], "booking_list": []},
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("busy" in w for w in resp.json()["warnings"])

    def test_update_missing_user_info_fields(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        state = {
            "n1": {"status": "exclusive", "current_users": [{"user_id": "u1"}], "booking_list": []},
            "n2": {"status": "idle", "current_users": [], "booking_list": []},
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("start_time" in w or "duration" in w for w in resp.json()["warnings"])

    def test_update_device_valid(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(
            client,
            admin_header,
            bot_type="DEVICE",
            cluster_configs={"n1": ["a100", "a100"]},
            tmp_path=tmp_path,
        )
        state = {
            "n1": [
                {"dev_id": 0, "dev_model": "a100", "status": "idle", "current_users": []},
                {"dev_id": 1, "dev_model": "a100", "status": "idle", "current_users": []},
            ]
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert resp.json().get("warnings", []) == []

    def test_update_device_count_mismatch(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(
            client, admin_header, bot_type="DEVICE", cluster_configs={"n1": ["a100", "a100", "h100"]}, tmp_path=tmp_path
        )
        state = {
            "n1": [
                {"dev_id": 0, "dev_model": "a100", "status": "idle", "current_users": []},
            ]
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("缺失" in w or "missing" in w for w in resp.json()["warnings"])

    def test_update_device_too_many(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(
            client,
            admin_header,
            bot_type="DEVICE",
            cluster_configs={"n1": ["a100"]},
            tmp_path=tmp_path,
        )
        state = {
            "n1": [
                {"dev_id": 0, "dev_model": "a100", "status": "idle", "current_users": []},
                {"dev_id": 1, "dev_model": "a100", "status": "idle", "current_users": []},
            ]
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        assert any("多余" in w or "excess" in w for w in resp.json()["warnings"])

    def test_update_device_model_synced(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(
            client,
            admin_header,
            bot_type="DEVICE",
            cluster_configs={"n1": ["h100"]},
            tmp_path=tmp_path,
        )
        state = {
            "n1": [
                {"dev_id": 0, "dev_model": "a100", "status": "idle", "current_users": []},
            ]
        }
        resp = client.put(f"/api/bots/{bot_id}/state", json=state, headers=admin_header)
        assert resp.status_code == 200
        # Fetch state to verify dev_model was synced
        resp2 = client.get(f"/api/bots/{bot_id}/state", headers=admin_header)
        devices = resp2.json()["n1"]
        assert devices[0]["dev_model"] == "h100"

    def test_update_not_a_dict(self, client, admin_header, tmp_path):
        bot_id = self._create_bot(client, admin_header, tmp_path=tmp_path)
        resp = client.put(f"/api/bots/{bot_id}/state", json="not a dict", headers=admin_header)
        # FastAPI rejects non-dict for body typed as dict
        assert resp.status_code == 422

    def test_update_running_bot_rejected(self, client, admin_header):
        from unittest.mock import patch

        bot_id = self._create_bot(client, admin_header)
        with patch("lockbot.backend.app.bots.router.bot_manager") as mock_mgr:
            mock_mgr.start_bot.return_value = 123
            client.post(f"/api/bots/{bot_id}/start", headers=admin_header)
        resp = client.put(f"/api/bots/{bot_id}/state", json={}, headers=admin_header)
        assert resp.status_code == 409

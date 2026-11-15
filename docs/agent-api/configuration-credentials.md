# Delegate bot configuration to an Agent

In the LockBot Console, open **API credentials** and create a configuration credential. Select the bots and grant **Register node connections**, **Configure data sources**, or both. Owners and ordinary administrators can select their own bots. Super administrators can select bots across owners. The Key appears on creation or rotation.

Store the Key privately in the Agent's environment and send it as a Bearer token to the LockBot instance that issued it. Keep the data source's authorization separate. Use the Console to rotate or revoke the Key. After ownership or bot identity changes, create a credential for the current bot.

## Delegate bot creation

An administrator or super administrator can choose **Create bots** as the credential purpose in the Console. The credential grants only `bot:create` and requires no existing bot selection. The administrator owns the new bot. A Feishu group route requires a super administrator and an existing Feishu application.

Download the standalone `lockbot_provision.py` from `/api/bot-provisioning/lockbot_provision.py` on that LockBot instance. It requires Python 3.10 or newer and uses the `LOCKBOT_PROVISIONING_KEY` environment variable. Store the proposed configuration in a private JSON file.

```sh
python3 lockbot_provision.py --service https://lockbot.example.test preview --config bot.json
# Display the preview and wait for explicit approval before running apply.
python3 lockbot_provision.py --service https://lockbot.example.test apply --config bot.json --receipt RECEIPT --confirm
```

The CLI resolves the returned Console path into `console_url`. The resource Agent Skill remains the entry point for querying, locking and releasing resources.

1. Send the proposed bot configuration to `POST /api/bot-provisioning/preview`. Show the returned platform, name, group, type and resource count to the user. Keep submitted platform secrets out of the conversation summary.
2. After the user's explicit approval, send `POST /api/bot-provisioning/apply` with `bot` set to the same configuration, the preview's `receipt`, and `confirmed` set to `true`. The receipt is valid for five minutes and is tied to the exact configuration and credential.
3. Return the `bot_id`, resolve `console_path` against the issuing LockBot origin, and report any `remaining_setup` items. `configure_infoflow_callback` means the user must add the LockBot callback in Infoflow. `check_bot_start` means the bot was saved but requires a start or quota check.

After a timeout, retry the same confirmed payload and receipt within the five-minute window. If the receipt expired and the result is uncertain, ask the user to check the Console for the proposed bot before obtaining a fresh approval.

## Review and apply a change

1. Call `GET /api/bots/{bot_id}/configuration-access` for the authoritative `node_ids`, `device_ids`, granted scopes, and accessible current configuration.
2. Call `POST /api/bots/{bot_id}/configuration-access/preview` with a proposal. Show the returned `before` and `after` to the user. Wait for explicit approval.
3. Call `PUT /api/bots/{bot_id}/configuration-access` with the same proposal and its `receipt`. The receipt expires after five minutes. If the service returns HTTP 409, request a fresh preview and approval before resubmitting.

A node connection proposal can update individual nodes. Omitted nodes keep their current settings. To remove a connection, name it in `remove_node_ids` and submit an empty `nodes` object when there are no other edits.

```json
{
  "scope": "node_access:write",
  "configuration": {
    "version": 2,
    "nodes": {
      "node-01": {
        "host": "compute-01.example.test",
        "ip": "192.0.2.41",
        "port": 2222
      }
    }
  }
}
```

A data source proposal contains the complete source configuration. To clear the configuration, set `configuration` to `null` and review the change. The preview validates the configuration and target resource IDs. Verify that the metrics endpoint is reachable separately from the browser or Agent that will use it.

```json
{
  "scope": "datasource:write",
  "configuration": {
    "endpoint": "https://metrics.example.test",
    "profile": "lockbot-v1",
    "accessMode": "short_lived",
    "nodeMapping": {
      "node-01": "collector-node-01"
    }
  }
}
```

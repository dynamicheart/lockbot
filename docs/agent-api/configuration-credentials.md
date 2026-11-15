# Delegate bot configuration to an Agent

In the LockBot Console, open **API credentials** and create a configuration credential. Select the bots and grant **Register node connections**, **Configure data sources**, or both. Owners and ordinary administrators can select their own bots. Super administrators can select bots across owners. The Key appears on creation or rotation.

Store the Key privately in the Agent's environment and send it as a Bearer token to the LockBot instance that issued it. Keep the data source's authorization separate. Use the Console to rotate or revoke the Key. After ownership or bot identity changes, create a credential for the current bot.

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

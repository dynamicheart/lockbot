# Configure LockBot nodes and data sources

The administrator supplies the LockBot service address and a configuration Key for one or more bots.
Read the Key from a private secret or file. Keep it out of commands, URLs, logs and sample data.

1. Send `GET /api/bots/{bot_id}/configuration-access` with the Key as a Bearer credential. Read
   `node_ids`, `device_ids`, the granted scopes and the saved configuration. If the bot is unclear, ask which one to use.
2. Show the node IDs. Ask which nodes to register and which approved login entry or monitoring source
   to use. Reuse answers the administrator already gave. For a shared login method, ask once for the batch.
   Only collect the selected nodes through the approved access. Report missing or conflicting mappings.
3. Use the matching scope. `node_access:write` registers verified SSH addresses and ports.
   `datasource:write` updates the metrics source and its mappings. Keep mappings for unselected nodes when
   updating an existing source. Changing a shared endpoint affects the whole bot, so confirm that change explicitly.
   `node_inventory:write` uploads observed hardware without changing SSH addresses, notes or metrics settings.
4. Send the payload to `POST /api/bots/{bot_id}/configuration-access/preview`. Show the differences and ask for
   approval before the first write. Submit the same payload with the returned `receipt` to
   `PUT /api/bots/{bot_id}/configuration-access`. If the receipt is stale, fetch and preview again.
5. Register hardware once and collect it again when the machine changes. Keep the approved node-to-source
   mapping in private configuration. LockBot records the observation time and retains the registered hardware
   until it receives a newer observation for that node.

## Hardware inventory payload

```json
{
  "scope": "node_inventory:write",
  "configuration": {
    "version": 1,
    "nodes": {
      "node-01": {
        "observed_at": "2026-01-01T00:00:00Z",
        "scope": "host",
        "accelerators": [{"model": "Example accelerator", "count": 4, "memory_bytes_per_card": 103079215104}],
        "memory_bytes": 274877906944,
        "cpu_arch": "x86_64"
      }
    }
  }
}
```

Use the collector observation time with a timezone. Set scope to `host`, `container` or `unknown`.
Group cards by model and per-card memory. Omit unknown capacities or architecture rather than guessing.
Upload only the documented fields. Use the private extension to handle hardware-specific private metadata.

## Upload an inventory file

Enable `inventory_output` in the existing exporter for the approved machines. Keep a private JSON
mapping keyed by LockBot node ID. Each value contains `source_id` from the exporter and the local
`path` to its inventory file, for example:

```json
{"node-01": {"source_id": "collector-node-a", "path": "/var/lib/lockbot-inventory/node-a.json"}}
```

Run `scripts/datasource/upload_node_inventory.py` with `--origin`, `--bot`, `--key-file` and `--mapping`
to preview. After the administrator confirms the mapping, run the same command with `--apply`.
When a snapshot is missing or unreadable, the uploader skips that node and keeps its previous record.

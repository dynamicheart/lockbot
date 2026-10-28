"""Agent-facing resource API.

Two routes, deliberately complementary rather than overlapping:

- ``POST /api/bots/{bot_id}/commands`` — one mutation language, an allowlisted free-text
  command, executed through the same dispatcher chat uses.
- ``GET /api/bots/{bot_id}/resources`` — one structured read, the authoritative view an
  agent verifies against.

The contract is deliberately "mutate, then read". A mutation response reports what the bot
*said*, not a structured outcome: unchanged state is ambiguous between idempotence,
rejection, invalid selection, ownership failure and a duration-limit failure, so inferring
an outcome by diffing state would be unsound. The snapshot proves what is true at
``captured_at``; it does not prove which command produced it.
"""

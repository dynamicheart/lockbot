"""Pure selector and untrusted read-position validation; no service or Key access."""

import base64
import json

import pytest

from backend.test_resource_agent_helper import helper

CATALOG = "a" * 64
QUERY = "b" * 64


def encoded(payload, **kwargs):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), **kwargs).encode()
    return "rl1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def invalid_cursor(token, total=3):
    with pytest.raises(helper.ClientError, match="^resource_list_cursor_invalid$"):
        helper.resource_list_cursor_start(token, CATALOG, QUERY, total)


def test_query_is_normalized_independent_and_can_be_validated_without_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Query and cursor validation must not open a connection or Key")

    monkeypatch.setattr(helper, "Client", forbidden)
    monkeypatch.setattr(helper, "private_bytes", forbidden)
    nodes, fields = ["NODE-2", "Straße", "node-2"], ["node", "hardware", "node"]
    query = helper.resource_list_query(nodes, "  P800  ", fields)
    assert query == {
        "nodes": ["node-2", "strasse"],
        "model": "p800",
        "fields": ["hardware", "node"],
        "limit": 20,
        "mine": False,
        "legacy_read": False,
    }
    assert nodes == ["NODE-2", "Straße", "node-2"] and fields == ["node", "hardware", "node"]
    nodes.clear()
    fields.clear()
    equivalent = helper.resource_list_query(["STRASSE", "node-2"], "p800", ["hardware", "node"])
    digest = helper.resource_list_query_digest(query)
    assert digest == helper.resource_list_query_digest(equivalent)
    assert helper.resource_list_query_digest(helper.resource_list_query(**query)) == digest
    token = helper.resource_list_cursor(CATALOG, digest, 0, "Exact-Node")
    assert helper.resource_list_cursor_start(token, CATALOG, digest, 1) == (0, "Exact-Node")


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"nodes": "n1"}, "nodes"),
        ({"nodes": None}, "nodes"),
        ({"nodes": [True]}, "nodes"),
        ({"nodes": [""]}, "nodes"),
        ({"nodes": ["line\nbreak"]}, "nodes"),
        ({"nodes": ["\ud800"]}, "nodes"),
        ({"nodes": ["x" * 257]}, "nodes"),
        ({"model": "   "}, "model"),
        ({"model": True}, "model"),
        ({"model": "P800\x00"}, "model"),
        ({"fields": []}, "fields"),
        ({"fields": "node"}, "fields"),
        ({"fields": ["unknown"]}, "fields"),
        ({"fields": [True]}, "fields"),
        ({"limit": True}, "limit"),
        ({"limit": 0}, "limit"),
        ({"limit": 257}, "limit"),
        ({"limit": 2.0}, "limit"),
        ({"mine": 1}, "profile"),
        ({"legacy_read": "yes"}, "profile"),
    ],
)
def test_invalid_selectors_fail_before_reading(kwargs, error):
    with pytest.raises(helper.ClientError, match=f"^resource_list_{error}_invalid$"):
        helper.resource_list_query(**kwargs)


@pytest.mark.parametrize(
    "changed",
    [
        {"nodes": ["other-node"]},
        {"model": "P900"},
        {"fields": ["node"]},
        {"limit": 21},
        {"mine": True},
        {"legacy_read": True},
    ],
)
def test_cursor_binds_every_effective_query_option(changed):
    digest = helper.resource_list_query_digest(helper.resource_list_query())
    token = helper.resource_list_cursor(CATALOG, digest, 0)
    other_digest = helper.resource_list_query_digest(helper.resource_list_query(**changed))
    assert digest != other_digest
    with pytest.raises(helper.ClientError, match="^resource_list_cursor_query_changed$"):
        helper.resource_list_cursor_start(token, CATALOG, other_digest, 1)


def test_cursor_payload_contains_only_version_hashes_and_position():
    query = helper.resource_list_query(["private-filter"], "private-model", ["node"])
    digest = helper.resource_list_query_digest(query)
    token = helper.resource_list_cursor(CATALOG, digest, 2, "node-42")
    payload = json.loads(base64.urlsafe_b64decode(token[4:] + "=" * (-len(token[4:]) % 4)))
    assert payload == [1, CATALOG, digest, 2, "node-42"]
    assert "private-filter" not in str(payload) and "private-model" not in str(payload)
    assert len(token) <= 2048 and "=" not in token


def test_catalog_change_is_distinct_even_when_the_new_catalog_has_fewer_records():
    token = helper.resource_list_cursor(CATALOG, QUERY, 3)
    with pytest.raises(helper.ClientError, match="^resource_list_cursor_catalog_changed$"):
        helper.resource_list_cursor_start(token, "c" * 64, QUERY, 1)


@pytest.mark.parametrize("node", ["机架-ß", "😀" * 256, 'node/"with\\punctuation'])
def test_printable_unicode_and_exact_node_ids_round_trip(node):
    token = helper.resource_list_cursor(CATALOG, QUERY, 1, node)
    assert len(token) <= 2048
    assert helper.resource_list_cursor_start(token, CATALOG, QUERY, 3) == (1, node)


def test_start_and_end_positions_are_distinct_and_end_cannot_resume_inside_a_scope():
    assert helper.resource_list_cursor_start(None, CATALOG, QUERY, 0) == (0, None)
    terminal = helper.resource_list_cursor(CATALOG, QUERY, 3)
    assert helper.resource_list_cursor_start(terminal, CATALOG, QUERY, 3) == (3, None)
    invalid_cursor(helper.resource_list_cursor(CATALOG, QUERY, 3, "node"))
    invalid_cursor(helper.resource_list_cursor(CATALOG, QUERY, 4))


@pytest.mark.parametrize(
    "token",
    [
        "",
        True,
        3,
        [],
        "sc1." + CATALOG + ".1",
        "rl2.bad",
        "rl1.",
        "rl1.a",
        "rl1.@@",
        "rl1." + "A" * 2045,
        "rl1.é",
        "rl1.\ud800",
        "rl1.W10=",
        encoded([True, CATALOG, QUERY, 0, None]),
        encoded([2, CATALOG, QUERY, 0, None]),
        encoded([1, CATALOG, QUERY, -1, None]),
        encoded([1, CATALOG, QUERY, True, None]),
        encoded([1, CATALOG, QUERY, 1.0, None]),
        encoded([1, CATALOG, QUERY, 0]),
        encoded({"version": 1}),
        encoded([1, CATALOG, QUERY, 0, ""]),
        encoded([1, CATALOG, QUERY, 0, "a\nb"]),
        encoded([1, CATALOG, QUERY, 0, "\u200d"]),
        encoded([1, CATALOG, QUERY, 0, "x" * 257]),
        encoded([1, "A" * 64, QUERY, 0, None]),
        encoded([1, CATALOG, "b" * 63, 0, None]),
        encoded([1, CATALOG, QUERY, 0, []]),
    ],
)
def test_malformed_or_out_of_contract_cursors_are_rejected(token):
    invalid_cursor(token)


def test_noncanonical_json_unicode_escapes_and_base64_pad_bits_are_rejected():
    canonical = helper.resource_list_cursor(CATALOG, QUERY, 10)
    payload = [1, CATALOG, QUERY, 0, "机"]
    escaped = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    invalid_cursor("rl1." + base64.urlsafe_b64encode(escaped).rstrip(b"=").decode())
    spaced = json.dumps([1, CATALOG, QUERY, 0, None]).encode()
    invalid_cursor("rl1." + base64.urlsafe_b64encode(spaced).rstrip(b"=").decode())
    invalid_cursor(canonical + "=")
    # The decoder accepts nonzero unused bits unless canonical re-encoding checks them.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(canonical[4:]) % 4 in (2, 3)
    changed = canonical[:-1] + alphabet[alphabet.index(canonical[-1]) + 1]
    assert base64.urlsafe_b64decode(changed[4:] + "=" * (-len(changed[4:]) % 4)) == base64.urlsafe_b64decode(
        canonical[4:] + "=" * (-len(canonical[4:]) % 4)
    )
    invalid_cursor(changed)
    surrogate = json.dumps([1, CATALOG, QUERY, 0, "\ud800"], ensure_ascii=True, separators=(",", ":")).encode()
    invalid_cursor("rl1." + base64.urlsafe_b64encode(surrogate).rstrip(b"=").decode())


@pytest.mark.parametrize("position", [-1, True, 1.0, "1"])
def test_encoder_rejects_invalid_positions(position):
    with pytest.raises(helper.ClientError, match="^resource_list_cursor_invalid$"):
        helper.resource_list_cursor(CATALOG, QUERY, position)


@pytest.mark.parametrize("total", [-1, True, 1.0])
def test_decoder_rejects_invalid_catalog_sizes_even_at_start(total):
    invalid_cursor(None, total)


def test_query_digest_and_cursor_encoder_reject_invalid_metadata():
    for query in ({}, {"unknown": 1}, []):
        with pytest.raises(helper.ClientError, match="^resource_list_query_invalid$"):
            helper.resource_list_query_digest(query)
    for fingerprint, digest in ((True, QUERY), (CATALOG, "F" * 64), ("short", QUERY)):
        with pytest.raises(helper.ClientError, match="^resource_list_cursor_invalid$"):
            helper.resource_list_cursor(fingerprint, digest, 0)
        with pytest.raises(helper.ClientError, match="^resource_list_cursor_invalid$"):
            helper.resource_list_cursor_start(None, fingerprint, digest, 0)


def test_normalized_query_size_is_bounded_in_utf8_before_io():
    nodes = [str(index) + "😀" * 125 for index in range(4)]
    query = helper.resource_list_query(nodes[:3])
    assert len(json.dumps(query, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    with pytest.raises(helper.ClientError, match="^resource_list_query_too_large$"):
        helper.resource_list_query(nodes)
    with pytest.raises(helper.ClientError, match="^resource_list_query_too_large$"):
        helper.resource_list_query([str(index) + "x" * 250 for index in range(8)])
    # Normalization/deduplication precedes the byte cap.
    assert helper.resource_list_query(["SAME-NODE"] * 1000)["nodes"] == ["same-node"]


def test_encoder_turns_oversized_integer_serialization_into_a_cursor_error():
    with pytest.raises(helper.ClientError, match="^resource_list_cursor_invalid$"):
        helper.resource_list_cursor(CATALOG, QUERY, 10**5000)


def test_decoder_rejects_non_utf8_json_and_invalid_utf8():
    for raw in (json.dumps([1, CATALOG, QUERY, 0, None]).encode("utf-16"), b"\xff"):
        invalid_cursor("rl1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode())


def test_casefold_expansion_preserves_canonical_selector_bounds():
    with pytest.raises(helper.ClientError, match="^resource_list_nodes_invalid$"):
        helper.resource_list_query(["ß" * 129])

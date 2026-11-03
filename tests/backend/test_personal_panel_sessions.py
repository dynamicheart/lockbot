import hashlib
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import lockbot.backend.app.personal_panel.sessions as module
import pytest
from lockbot.backend.app.personal_panel.sessions import PanelSessions


def grant(**updates):
    return (
        dict(
            issuer="synthetic-service-a",
            platform="synthetic-im",
            realm="synthetic-tenant-a",
            subject="synthetic-user-a",
            scope="synthetic-group-a",
            ref="synthetic-binding-a",
        )
        | updates
    )


def test_prefetch_persistence_single_use_and_logout(tmp_path):
    path = tmp_path / "sessions.sqlite"
    sessions = PanelSessions(path)
    ticket = sessions.issue(grant())
    assert sessions.peek(ticket) == sessions.peek(ticket) == grant()
    assert sessions.grants(None) == []
    sessions.close()
    sessions = PanelSessions(path)
    cookie = sessions.consume(ticket)
    assert sessions.grants(cookie) == [grant()]
    with pytest.raises(ValueError):
        sessions.consume(ticket)
    with pytest.raises(ValueError):
        sessions.peek(ticket)
    sessions.close()
    sessions = PanelSessions(path)
    assert sessions.grants(cookie) == [grant()]
    sessions.logout(cookie)
    sessions.logout(cookie)
    assert sessions.grants(cookie) == []
    sessions.close()
    sessions = PanelSessions(path)
    assert sessions.grants(cookie) == []
    sessions.close()


def test_exact_ticket_and_session_expiry_and_prune(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    ticket = sessions.issue(grant())
    now[0] += module.TICKET_LIFETIME - 0.001
    assert sessions.peek(ticket) == grant()
    now[0] += 0.001
    with pytest.raises(ValueError):
        sessions.peek(ticket)
    with pytest.raises(ValueError):
        sessions.consume(ticket)
    assert sessions.prune() == {"tickets": 1, "sessions": 0}
    cookie = sessions.consume(sessions.issue(grant()))
    now[0] += module.SESSION_LIFETIME - 0.001
    assert sessions.grants(cookie) == [grant()]
    now[0] += 0.001
    assert sessions.grants(cookie) == []
    assert sessions.db.execute("SELECT count(*) FROM panel_sessions").fetchone()[0] == 0
    sessions.close()


def test_default_replace_rotates_and_keeps_other_browser_independent(tmp_path):
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    alice = sessions.consume(sessions.issue(grant()))
    other_browser = sessions.consume(sessions.issue(grant()))
    bob_grant = grant(subject="synthetic-user-b", ref="synthetic-binding-b")
    bob = sessions.consume(sessions.issue(bob_grant), current_cookie=alice)
    assert alice != bob
    assert sessions.grants(alice) == []
    assert sessions.grants(bob) == [bob_grant]
    assert sessions.grants(other_browser) == [grant()]
    sessions.close()


def test_explicit_append_requires_existing_session_and_fresh_proof(tmp_path):
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    ticket = sessions.issue(grant())
    for current_cookie in (None, "invalid" * 8):
        with pytest.raises(ValueError):
            sessions.consume(ticket, current_cookie=current_cookie, mode="append")
    assert sessions.peek(ticket) == grant()
    first = sessions.consume(ticket)
    second_grant = grant(platform="synthetic-other-im", ref="synthetic-binding-b")
    proof = sessions.issue(second_grant)
    with pytest.raises(ValueError):
        sessions.consume(proof, first, mode="merge")
    second = sessions.consume(proof, first, mode="append")
    assert second != first
    assert sessions.grants(first) == []
    assert sessions.grants(second) == [grant(), second_grant]
    assert all(set(binding) == set(grant()) for binding in sessions.grants(second))
    with pytest.raises(ValueError):
        sessions.consume(proof, second, mode="append")
    # Stale cookies cannot be used to append, even with a new valid proof.
    with pytest.raises(ValueError):
        sessions.consume(sessions.issue(grant()), first, mode="append")
    sessions.close()


@pytest.mark.parametrize("field", ["issuer", "platform", "realm", "subject", "scope", "ref"])
def test_same_strings_never_merge_distinct_bindings(tmp_path, field):
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    first = grant()
    second = grant(**{field: "different-synthetic-value"})
    cookie = sessions.consume(sessions.issue(first))
    cookie = sessions.consume(sessions.issue(second), cookie, mode="append")
    assert sessions.grants(cookie) == [first, second]
    cookie = sessions.consume(sessions.issue(first), cookie, mode="append")
    assert sessions.grants(cookie) == [first, second]
    sessions.close()


def test_append_limit_is_atomic_and_does_not_consume_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_GRANTS", 2)
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    cookie = sessions.consume(sessions.issue(grant()))
    second = grant(ref="synthetic-second-binding")
    cookie = sessions.consume(sessions.issue(second), cookie, mode="append")
    third = grant(ref="synthetic-third-binding")
    ticket = sessions.issue(third)
    with pytest.raises(ValueError, match="limit"):
        sessions.consume(ticket, cookie, mode="append")
    assert sessions.grants(cookie) == [grant(), second]
    assert sessions.peek(ticket) == third
    replacement = sessions.consume(ticket, cookie)
    assert sessions.grants(cookie) == []
    assert sessions.grants(replacement) == [third]
    sessions.close()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"key": "synthetic-secret"},
        grant(key="synthetic-secret"),
        grant(subject=""),
        grant(scope=[]),
        grant(ref=None),
        grant(issuer=42),
        grant(realm=" "),
        grant(ref="x" * 2049),
    ],
)
def test_grant_allowlist_rejects_credentials_and_invalid_identity(tmp_path, bad):
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    with pytest.raises(ValueError):
        sessions.issue(bad)
    assert sessions.db.execute("SELECT count(*) FROM panel_tickets").fetchone()[0] == 0
    sessions.close()


@pytest.mark.parametrize("bad", [None, "", "x", "x" * 129, 17, [], "\ud800" * 40])
def test_invalid_bearers_fail_without_reflecting_values(tmp_path, bad):
    sessions = PanelSessions(tmp_path / "sessions.sqlite")
    assert sessions.grants(bad) == []
    sessions.logout(bad)
    with pytest.raises(ValueError, match="Invalid or expired handoff"):
        sessions.peek(bad)
    with pytest.raises(ValueError, match="Invalid or expired handoff"):
        sessions.consume(bad)
    sessions.close()


def test_only_digests_and_reference_metadata_are_persisted(tmp_path):
    path = tmp_path / "sessions.sqlite"
    sessions = PanelSessions(path)
    ticket = sessions.issue(grant())
    stored_ticket = sessions.db.execute("SELECT digest,grant_json FROM panel_tickets").fetchone()
    assert stored_ticket == (
        hashlib.sha256(ticket.encode()).hexdigest(),
        json.dumps(grant(), sort_keys=True, separators=(",", ":")),
    )
    cookie = sessions.consume(ticket)
    stored_session = sessions.db.execute("SELECT digest,grants_json FROM panel_sessions").fetchone()
    assert stored_session[0] == hashlib.sha256(cookie.encode()).hexdigest()
    assert json.loads(stored_session[1]) == [grant()]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    sessions.close()
    assert ticket.encode() not in path.read_bytes()
    assert cookie.encode() not in path.read_bytes()


@pytest.mark.parametrize("shared_connection", [True, False])
def test_simultaneous_consumers_have_exactly_one_winner(tmp_path, shared_connection):
    path = tmp_path / "sessions.sqlite"
    sessions = PanelSessions(path)
    ticket = sessions.issue(grant())
    peers = [sessions if shared_connection else PanelSessions(path) for _ in range(8)]
    barrier = threading.Barrier(len(peers))

    def consume(peer):
        barrier.wait()
        try:
            return peer.consume(ticket)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=len(peers)) as executor:
        cookies = list(executor.map(consume, peers))
    winners = [cookie for cookie in cookies if cookie is not None]
    assert len(winners) == 1
    assert sessions.grants(winners[0]) == [grant()]
    assert sessions.db.execute("SELECT count(*) FROM panel_sessions").fetchone()[0] == 1
    for peer in set(peers + [sessions]):
        peer.close()


def test_simultaneous_append_cannot_resurrect_rotated_cookie(tmp_path):
    path = tmp_path / "sessions.sqlite"
    sessions = PanelSessions(path)
    cookie = sessions.consume(sessions.issue(grant()))
    peers = [PanelSessions(path), PanelSessions(path)]
    proofs = [sessions.issue(grant(ref="synthetic-binding-" + str(i))) for i in range(2)]
    barrier = threading.Barrier(2)

    def append(index):
        barrier.wait()
        try:
            return peers[index].consume(proofs[index], cookie, mode="append")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, range(2)))
    assert sum(result is not None for result in results) == 1
    assert sessions.grants(cookie) == []
    for index, result in enumerate(results):
        if result:
            assert sessions.grants(result) == [grant(), grant(ref="synthetic-binding-" + str(index))]
        else:
            assert sessions.peek(proofs[index]) == grant(ref="synthetic-binding-" + str(index))
    for peer in peers + [sessions]:
        peer.close()

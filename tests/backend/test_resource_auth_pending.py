"""AC-2 / AC-4 / AC-12: the pending store and the credential primitives.

These are pure unit tests: no database, no HTTP. They pin the properties the rest of the
binding design depends on — single-use claims, expiry enforced at claim time, bounded
memory, and digest-only storage.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from lockbot.backend.app.resource_auth import credentials
from lockbot.backend.app.resource_auth.pending import (
    InProcessPendingBindingStore,
    PendingCapExceeded,
    PendingCollision,
)

WINDOW = 600
CAPS = {"max_total": 100, "max_per_source": 5}


def _now():
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return InProcessPendingBindingStore()


def _create(store, code="CODE1234", *, source="1.2.3.4", now=None, binding_id=None, label=None, window=WINDOW):
    return store.create(
        binding_id=binding_id or credentials.generate_public_id(),
        code_digest=credentials.digest_bind_code(code),
        credential_digest=credentials.digest_credential("lbr_" + code),
        source=source,
        label=label,
        window_seconds=window,
        now=now or _now(),
        **CAPS,
    )


class TestClaim:
    def test_claim_returns_the_record(self, store):
        record = _create(store)
        claimed = store.claim(credentials.digest_bind_code("CODE1234"), now=_now())
        assert claimed is not None
        assert claimed.binding_id == record.binding_id

    def test_claim_is_single_use(self, store):
        _create(store)
        digest = credentials.digest_bind_code("CODE1234")
        assert store.claim(digest, now=_now()) is not None
        assert store.claim(digest, now=_now()) is None

    def test_unknown_code_claims_nothing(self, store):
        _create(store)
        assert store.claim(credentials.digest_bind_code("NOPE2345"), now=_now()) is None

    def test_claim_is_case_and_padding_insensitive(self, store):
        _create(store, code="CODE1234")
        assert store.claim(credentials.digest_bind_code("  code1234 "), now=_now()) is not None

    def test_concurrent_claims_produce_exactly_one_success(self, store):
        """AC-2: two concurrent confirmations of one code produce exactly one success."""
        _create(store)
        digest = credentials.digest_bind_code("CODE1234")
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(store.claim(digest, now=_now()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for r in results if r is not None) == 1


class TestReservation:
    """B13: reserve/finalize/restore so a failed durable write never strands a code."""

    def test_reserve_hides_the_code_from_a_concurrent_claim(self, store):
        _create(store, binding_id="b1")
        digest = credentials.digest_bind_code("CODE1234")
        record, token = store.reserve(digest, now=_now())
        assert record.binding_id == "b1"
        # While reserved the code is not claimable: a second claimant gets nothing.
        assert store.reserve(digest, now=_now()) is None
        assert store.claim(digest, now=_now()) is None
        # But it is still an outstanding record: get() reports it (so status reads pending, and
        # it keeps its cap slot), because the durable write may yet commit or be rolled back.
        assert store.get("b1", now=_now()) is not None
        assert token

    def test_finalize_drops_the_record_permanently(self, store):
        _create(store, binding_id="b1")
        digest = credentials.digest_bind_code("CODE1234")
        _, token = store.reserve(digest, now=_now())
        store.finalize(token)
        store.restore(token)  # a finalized token cannot be resurrected
        assert store.get("b1", now=_now()) is None
        assert store.claim(digest, now=_now()) is None

    def test_restore_returns_the_code_to_the_claimable_set(self, store):
        record = _create(store, binding_id="b1")
        digest = credentials.digest_bind_code("CODE1234")
        _, token = store.reserve(digest, now=_now())
        store.restore(token)
        again = store.claim(digest, now=_now())
        assert again is not None
        assert again.binding_id == "b1"
        assert again.bind_expires_at == record.bind_expires_at  # original deadline preserved

    def test_restore_after_the_window_closed_is_treated_as_expired(self, store):
        _create(store, now=_now())
        digest = credentials.digest_bind_code("CODE1234")
        _, token = store.reserve(digest, now=_now())
        store.restore(token)
        assert store.claim(digest, now=_now() + timedelta(seconds=WINDOW + 1)) is None

    def test_reserve_of_an_unknown_code_is_none(self, store):
        assert store.reserve(credentials.digest_bind_code("NOPE2345"), now=_now()) is None

    def test_finalize_and_restore_of_an_unknown_token_are_no_ops(self, store):
        store.finalize("not-a-token")
        store.restore("not-a-token")


class TestReservationHonoursCaps:
    """B15: a reserved record stays an outstanding record — it keeps its cap slot and id/code."""

    def _create_one(self, store, *, code="CODE0001", binding_id="b1", source="1.1.1.1", max_total=1, max_per_source=5):
        return store.create(
            binding_id=binding_id,
            code_digest=credentials.digest_bind_code(code),
            credential_digest="d",
            source=source,
            window_seconds=WINDOW,
            max_total=max_total,
            max_per_source=max_per_source,
            now=_now(),
        )

    def test_reserved_record_still_counts_toward_the_global_cap(self, store):
        self._create_one(store, code="CODE0001", binding_id="b1", max_total=1)
        store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        # The slot is still taken while the confirmation is in flight.
        with pytest.raises(PendingCapExceeded) as exc:
            self._create_one(store, code="CODE0002", binding_id="b2", max_total=1)
        assert exc.value.scope == "global"
        assert store.count(now=_now()) == 1

    def test_reserved_record_still_counts_toward_the_per_source_cap(self, store):
        self._create_one(store, code="CODE0001", binding_id="b1", source="9.9.9.9", max_total=10, max_per_source=1)
        store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        with pytest.raises(PendingCapExceeded) as exc:
            self._create_one(store, code="CODE0002", binding_id="b2", source="9.9.9.9", max_total=10, max_per_source=1)
        assert exc.value.scope == "per-source"

    def test_restore_never_lets_count_exceed_the_cap(self, store):
        """The exact probe from the review: cap of one must never become two."""
        self._create_one(store, code="CODE0001", binding_id="b1", max_total=1)
        _, token = store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        store.restore(token)
        assert store.count(now=_now()) == 1

    def test_finalize_frees_the_cap_slot(self, store):
        self._create_one(store, code="CODE0001", binding_id="b1", max_total=1)
        _, token = store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        store.finalize(token)
        # The slot is free now, so a fresh issuance succeeds and the count stays at one.
        self._create_one(store, code="CODE0002", binding_id="b2", max_total=1)
        assert store.count(now=_now()) == 1

    def test_issuance_colliding_with_a_reserved_id_or_code_is_rejected(self, store):
        self._create_one(store, code="CODE0001", binding_id="b1", max_total=10)
        store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        with pytest.raises(PendingCollision):  # same code digest as the reserved record
            self._create_one(store, code="CODE0001", binding_id="b2", max_total=10)
        with pytest.raises(PendingCollision):  # same binding id as the reserved record
            self._create_one(store, code="CODE0002", binding_id="b1", max_total=10)

    def test_get_and_count_report_a_reserved_record_as_outstanding(self, store):
        self._create_one(store, code="CODE0001", binding_id="b1", max_total=10)
        store.reserve(credentials.digest_bind_code("CODE0001"), now=_now())
        assert store.count(now=_now()) == 1
        assert store.get("b1", now=_now()) is not None


class TestWindow:
    def test_confirmation_just_inside_the_window_succeeds(self, store):
        _create(store, now=_now())
        just_inside = _now() + timedelta(seconds=WINDOW - 1)
        assert store.claim(credentials.digest_bind_code("CODE1234"), now=just_inside) is not None

    def test_confirmation_just_past_the_window_fails(self, store):
        _create(store, now=_now())
        just_past = _now() + timedelta(seconds=WINDOW + 1)
        assert store.claim(credentials.digest_bind_code("CODE1234"), now=just_past) is None

    def test_window_is_enforced_at_claim_time_not_by_a_sweep(self, store):
        """A pruning job that never runs cannot revive an expired code."""
        _create(store, now=_now())
        # No pruning call of any kind between create and claim.
        assert store.claim(credentials.digest_bind_code("CODE1234"), now=_now() + timedelta(days=30)) is None

    def test_expired_record_is_not_reported_as_pending(self, store):
        record = _create(store, now=_now())
        assert store.get(record.binding_id, now=_now()) is not None
        assert store.get(record.binding_id, now=_now() + timedelta(seconds=WINDOW + 1)) is None


class TestCaps:
    def test_per_source_cap_is_enforced(self, store):
        for i in range(CAPS["max_per_source"]):
            _create(store, code=f"CODE{i:04d}", source="9.9.9.9")
        with pytest.raises(PendingCapExceeded) as exc:
            _create(store, code="CODEFFFF", source="9.9.9.9")
        assert exc.value.scope == "per-source"

    def test_a_different_source_is_unaffected_by_another_sources_cap(self, store):
        for i in range(CAPS["max_per_source"]):
            _create(store, code=f"CODE{i:04d}", source="9.9.9.9")
        assert _create(store, code="CODEFFFF", source="8.8.8.8") is not None

    def test_global_cap_is_enforced(self):
        small = InProcessPendingBindingStore()
        for i in range(3):
            small.create(
                binding_id=f"b{i}",
                code_digest=credentials.digest_bind_code(f"CODE{i:04d}"),
                credential_digest="d",
                source=f"src{i}",
                window_seconds=WINDOW,
                max_total=3,
                max_per_source=99,
                now=_now(),
            )
        with pytest.raises(PendingCapExceeded) as exc:
            small.create(
                binding_id="b-overflow",
                code_digest=credentials.digest_bind_code("CODEFFFF"),
                credential_digest="d",
                source="src-overflow",
                window_seconds=WINDOW,
                max_total=3,
                max_per_source=99,
                now=_now(),
            )
        assert exc.value.scope == "global"

    def test_expired_records_are_pruned_on_access_so_memory_cannot_grow(self, store):
        for i in range(CAPS["max_per_source"]):
            _create(store, code=f"CODE{i:04d}", source="7.7.7.7", now=_now())
        later = _now() + timedelta(seconds=WINDOW + 1)
        # The cap would reject this if expired records were not pruned first.
        assert _create(store, code="CODEFFFF", source="7.7.7.7", now=later) is not None
        assert store.count(now=later) == 1


class TestCollision:
    """Q1: a generated id or code that already exists must never silently overwrite."""

    def test_duplicate_code_is_rejected_and_the_original_survives(self, store):
        _create(store, code="CODE1234", binding_id="first")
        with pytest.raises(PendingCollision):
            _create(store, code="CODE1234", binding_id="second")
        # The first binding is untouched and still claimable.
        claimed = store.claim(credentials.digest_bind_code("CODE1234"), now=_now())
        assert claimed is not None
        assert claimed.binding_id == "first"

    def test_duplicate_binding_id_is_rejected_and_the_original_survives(self, store):
        _create(store, code="CODE1234", binding_id="same-id")
        with pytest.raises(PendingCollision):
            _create(store, code="CODE5678", binding_id="same-id")
        assert store.get("same-id", now=_now()) is not None
        # The clobbering code was never recorded.
        assert store.claim(credentials.digest_bind_code("CODE5678"), now=_now()) is None

    def test_a_collision_does_not_consume_cap_space(self, store):
        _create(store, code="CODE1234", binding_id="first", source="1.1.1.1")
        with pytest.raises(PendingCollision):
            _create(store, code="CODE1234", binding_id="second", source="1.1.1.1")
        assert store.count(now=_now()) == 1


class TestIssuanceRetriesOnCollision:
    """``create_binding`` regenerates on a collision rather than failing or overwriting."""

    def test_two_issuances_colliding_on_code_both_survive(self, monkeypatch):
        from lockbot.backend.app.resource_auth import service
        from lockbot.backend.app.resource_auth.pending import pending_store

        codes = iter(["DUPCODE1", "DUPCODE1", "UNIQUE23"])
        monkeypatch.setattr(service.credentials, "generate_bind_code", lambda: next(codes))

        first = service.create_binding(label="a")
        second = service.create_binding(label="b")  # first code collides, retry succeeds

        assert first["binding_id"] != second["binding_id"]
        assert first["bind_code"] != second["bind_code"]
        for issued in (first, second):
            assert pending_store.get(issued["binding_id"]) is not None


class TestNoSecretsHeld:
    def test_record_holds_digests_only(self, store):
        record = _create(store, code="CODE1234")
        serialized = repr(record)
        assert "CODE1234" not in serialized
        assert "lbr_CODE1234" not in serialized
        assert record.code_digest == credentials.digest_bind_code("CODE1234")

    def test_purge_all_drops_everything(self, store):
        _create(store, code="CODE1234")
        _create(store, code="CODE5678")
        store.purge_all()
        assert store.count() == 0
        assert store.claim(credentials.digest_bind_code("CODE1234"), now=_now()) is None


class TestCredentialPrimitives:
    def test_credentials_are_prefixed_and_unique(self):
        issued = {credentials.generate_credential() for _ in range(50)}
        assert len(issued) == 50
        assert all(c.startswith(credentials.CREDENTIAL_PREFIX) for c in issued)

    def test_legacy_key_shape_is_not_mistaken_for_a_credential(self):
        assert not credentials.looks_like_credential("lbk_" + "a" * 48)
        assert credentials.looks_like_credential(credentials.generate_credential())

    def test_digest_is_not_reversible_and_matches(self):
        credential = credentials.generate_credential()
        digest = credentials.digest_credential(credential)
        assert credential not in digest
        assert len(digest) == 64
        assert credentials.credential_matches(digest, credential)

    def test_wrong_credential_does_not_match(self):
        digest = credentials.digest_credential(credentials.generate_credential())
        assert not credentials.credential_matches(digest, credentials.generate_credential())

    @pytest.mark.parametrize(("digest", "candidate"), [("", "x"), ("d", ""), ("", "")])
    def test_empty_inputs_never_match(self, digest, candidate):
        assert not credentials.credential_matches(digest, candidate)

    def test_bind_codes_avoid_ambiguous_characters(self):
        for _ in range(50):
            code = credentials.generate_bind_code()
            assert len(code) == 8
            assert not set(code) & set("01OILB8")

    def test_bind_codes_are_not_trivially_repeated(self):
        assert len({credentials.generate_bind_code() for _ in range(200)}) > 190

    def test_short_id_is_derived_from_the_public_id(self):
        public_id = credentials.generate_public_id()
        assert credentials.short_id(public_id) == public_id[:8]
        assert len(credentials.short_id(public_id)) == 8

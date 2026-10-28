"""AC-12: a second process must fail fast rather than corrupt shared state.

The design depends on single-process operation in two places — in-memory lock state and the
in-process pending-binding store — so this is a correctness guard, not hygiene.
"""

import os
import subprocess
import sys
import textwrap

import pytest
from lockbot.backend.app.state_ownership import (
    LOCK_FILENAME,
    acquire_state_store_ownership,
    owns_state_store,
    release_state_store_ownership,
)


@pytest.fixture(autouse=True)
def _release_after_each():
    yield
    release_state_store_ownership()


def test_ownership_is_acquired_and_creates_the_lock_file(tmp_path):
    path = acquire_state_store_ownership(str(tmp_path))
    assert os.path.basename(path) == LOCK_FILENAME
    assert os.path.exists(path)
    assert owns_state_store()


def test_lock_file_records_the_owning_pid(tmp_path):
    path = acquire_state_store_ownership(str(tmp_path))
    with open(path) as handle:
        assert f"pid={os.getpid()}" in handle.read()


def test_acquiring_twice_in_one_process_is_idempotent(tmp_path):
    first = acquire_state_store_ownership(str(tmp_path))
    second = acquire_state_store_ownership(str(tmp_path))
    assert first == second


def test_a_missing_data_dir_is_created(tmp_path):
    target = tmp_path / "nested" / "data"
    acquire_state_store_ownership(str(target))
    assert target.is_dir()


def test_release_gives_up_ownership(tmp_path):
    acquire_state_store_ownership(str(tmp_path))
    release_state_store_ownership()
    assert not owns_state_store()
    # And it can be taken again afterwards.
    acquire_state_store_ownership(str(tmp_path))
    assert owns_state_store()


def test_a_second_process_is_refused(tmp_path):
    """The decisive case: a genuinely separate process, not a second call in this one."""
    acquire_state_store_ownership(str(tmp_path))

    script = textwrap.dedent(
        """
        import sys
        from lockbot.backend.app.state_ownership import (
            StateStoreOwnershipError,
            acquire_state_store_ownership,
        )

        try:
            acquire_state_store_ownership(sys.argv[1])
        except StateStoreOwnershipError as exc:
            print(str(exc))
            sys.exit(3)
        sys.exit(0)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )

    assert result.returncode == 3, f"second process was not refused: {result.stdout}{result.stderr}"
    # The message has to tell the operator *why* a single process is required.
    assert "single process" in result.stdout
    assert "pending-binding store" in result.stdout
    assert "DATA_DIR" in result.stdout


def test_a_second_process_succeeds_once_the_first_releases(tmp_path):
    acquire_state_store_ownership(str(tmp_path))
    release_state_store_ownership()

    script = (
        "import sys; from lockbot.backend.app.state_ownership import acquire_state_store_ownership as a; a(sys.argv[1])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )
    assert result.returncode == 0, result.stderr


def test_a_different_data_dir_is_independent(tmp_path):
    acquire_state_store_ownership(str(tmp_path / "a"))

    script = (
        "import sys; from lockbot.backend.app.state_ownership import acquire_state_store_ownership as a; a(sys.argv[1])"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "b")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
    )
    assert result.returncode == 0, result.stderr

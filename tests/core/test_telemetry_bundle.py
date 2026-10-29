"""Release bundles verify upstream bytes and never extract untrusted archive paths."""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "telemetry_bundle", Path(__file__).resolve().parents[2] / "scripts/telemetry/build_bundle.py"
)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def archive(path, *, symlink=False):
    prefix = "prometheus-3.7.3.linux-amd64/"
    with tarfile.open(path, "w:gz") as target:
        for name in ("prometheus", "promtool", "LICENSE", "NOTICE"):
            entry = tarfile.TarInfo(prefix + name)
            if symlink and name == "prometheus":
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                target.addfile(entry)
            else:
                value = b"fixture-only"
                entry.size = len(value)
                target.addfile(entry, io.BytesIO(value))
        entry = tarfile.TarInfo("../../outside")
        entry.size = 1
        target.addfile(entry, io.BytesIO(b"x"))


def test_only_explicit_program_and_license_members_are_unpacked(tmp_path):
    upstream = tmp_path / "upstream.tar.gz"
    archive(upstream)
    output = tmp_path / "output"
    output.mkdir()
    bundle.unpack_programs(upstream, output, "3.7.3", "linux-amd64")
    assert {p.name for p in output.iterdir()} == {"prometheus", "promtool", "LICENSE", "NOTICE"}
    assert not (tmp_path / "outside").exists()


def test_symlink_cannot_replace_a_verified_binary(tmp_path):
    upstream = tmp_path / "upstream.tar.gz"
    archive(upstream, symlink=True)
    with pytest.raises(ValueError):
        bundle.unpack_programs(upstream, tmp_path, "3.7.3", "linux-amd64")


def test_mismatched_upstream_checksum_stops_before_unpack_or_pip(tmp_path, monkeypatch):
    def download(url, path):
        if url.endswith("sha256sums.txt"):
            path.write_text("0" * 64 + "  prometheus-3.7.3.linux-amd64.tar.gz\n")
        else:
            path.write_bytes(b"wrong upstream bytes")

    monkeypatch.setattr(bundle, "download", download)
    with pytest.raises(ValueError, match="checksum mismatch"):
        bundle.build(tmp_path / "output", "linux-amd64", "preview-test")
    assert not list((tmp_path / "output").glob("*.tar.gz"))

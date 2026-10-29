#!/usr/bin/env python3
"""Build an explicit, secret-free Linux deployment bundle from official releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMETHEUS_VERSION = "3.7.3"  # Reproducible tested baseline, not a claim of latest.
PYJWT_VERSION = "2.15.1"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url, path):
    with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def unpack_programs(archive, destination, version, platform):
    prefix = f"prometheus-{version}.{platform}/"
    with tarfile.open(archive) as source:
        for name in ("prometheus", "promtool", "LICENSE", "NOTICE"):
            member = source.getmember(prefix + name)
            if not member.isfile():
                raise ValueError("Expected regular upstream program or license")
            target = destination / name
            with source.extractfile(member) as data, target.open("wb") as output:
                shutil.copyfileobj(data, output)
            target.chmod(0o755 if name in {"prometheus", "promtool"} else 0o644)


def build(output, platform, version):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("Invalid bundle version")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        name = f"lockbot-telemetry-{version}-{platform}"
        bundle = temp / name
        bundle.mkdir()
        archive_name = f"prometheus-{PROMETHEUS_VERSION}.{platform}.tar.gz"
        upstream = f"https://github.com/prometheus/prometheus/releases/download/v{PROMETHEUS_VERSION}"
        download(upstream + "/sha256sums.txt", temp / "upstream-sha256sums.txt")
        hashes = {
            parts[1].lstrip("*"): parts[0]
            for line in (temp / "upstream-sha256sums.txt").read_text().splitlines()
            if len(parts := line.split()) == 2
        }
        expected = hashes[archive_name]
        download(upstream + "/" + archive_name, temp / archive_name)
        if digest(temp / archive_name) != expected:
            raise ValueError("Official Prometheus checksum mismatch")
        unpack_programs(temp / archive_name, bundle, PROMETHEUS_VERSION, platform)
        shutil.copy2(ROOT / "LICENSE", bundle / "LOCKBOT-LICENSE")
        for module in ("exporter.py", "gateway.py"):
            shutil.copy2(ROOT / "python/lockbot/metrics" / module, bundle / module)
        shutil.copytree(ROOT / "docs/examples/resource-exporter", bundle / "examples")
        shutil.copy2(ROOT / "docs/usage/resource-exporter.md", bundle / "RESOURCE-EXPORTER.md")
        shutil.copy2(ROOT / "docs/usage/telemetry-deployment.md", bundle / "AGENT-DEPLOY.md")
        shutil.copy2(ROOT / "scripts/telemetry/prepare.sh", bundle / "prepare.sh")
        (bundle / "requirements.txt").write_text(f"PyJWT=={PYJWT_VERSION}\n")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--no-deps",
                "--dest",
                str(bundle / "wheels"),
                f"PyJWT=={PYJWT_VERSION}",
            ],
            check=True,
        )
        # Each service runs from the unpacked directory after the operator stages it.
        for unit in (bundle / "examples").glob("*.service"):
            text = unit.read_text()
            text = text.replace("/usr/bin/python3", "/opt/lockbot-telemetry/.venv/bin/python")
            unit.write_text(text)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        manifest = {
            "version": version,
            "source_commit": commit,
            "source_dirty": bool(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
            ),
            "platform": platform,
            "python": ">=3.10 (collector and SSH targets)",
            "prometheus": {"version": PROMETHEUS_VERSION, "url": upstream + "/" + archive_name, "sha256": expected},
            "gateway_dependency": f"PyJWT=={PYJWT_VERSION}",
            "files": {
                str(path.relative_to(bundle)): digest(path) for path in sorted(bundle.rglob("*")) if path.is_file()
            },
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        artifact = output / (name + ".tar.gz")
        with tarfile.open(artifact, "w:gz") as target:
            target.add(bundle, arcname=name)
        (output / (artifact.name + ".sha256")).write_text(digest(artifact) + "  " + artifact.name + "\n")
        print(artifact)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux-amd64", "linux-arm64"), default="linux-amd64")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    build(args.output.resolve(), args.platform, args.version)

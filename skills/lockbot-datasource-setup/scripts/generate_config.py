#!/usr/bin/env python3
"""Generate non-secret, reviewable datasource settings; never contact a service."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

FIELDS = {
    "profile",
    "name",
    "endpoint",
    "access_mode",
    "node_mapping",
    "device_mapping",
    "interval_seconds",
    "stale_seconds",
}


def text(value, label, limit=256):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError(f"Invalid {label}")
    return value


def mapping(value, label):
    if not isinstance(value, dict) or len(value) > 512:
        raise ValueError(f"Invalid {label}")
    return {text(k, label): text(v, label) for k, v in value.items()}


def generate(source):
    if not isinstance(source, dict) or set(source) - FIELDS:
        raise ValueError("Only documented public input fields are allowed; never include credentials")
    endpoint = text(source.get("endpoint"), "endpoint", 2048)
    url = urlsplit(endpoint)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or "\\" in endpoint
        or any(c.isspace() for c in endpoint)
    ):
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query or fragment")
    _ = url.port  # Reject malformed or out-of-range ports.
    nodes = mapping(source.get("node_mapping"), "node mapping")
    if not nodes:
        raise ValueError("At least one verified node mapping is required")
    devices = mapping(source.get("device_mapping", {}), "device mapping")
    for resource in devices:
        node, sep, index = resource.rpartition(":dev")
        if not sep or node not in nodes or not index.isascii() or not index.isdigit():
            raise ValueError("Device resource IDs must use a mapped node followed by :dev<number>")
    for node in nodes:
        device_ids = [v for k, v in devices.items() if k.rpartition(":dev")[0] == node]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("Two resource devices cannot map to the same source device within a node")
    profile = source.get("profile", "lockbot-v1")
    mode = source.get("access_mode", "independent")
    if profile not in ("lockbot-v1", "dcgm") or mode not in ("independent", "short_lived"):
        raise ValueError("Unsupported profile or access mode; generic JSON adapters are not installed by this script")
    interval, stale = source.get("interval_seconds", 300), source.get("stale_seconds", 660)
    if any(type(v) is not int or not 30 <= v <= 3600 for v in (interval, stale)) or stale < interval:
        raise ValueError("Refresh/stale seconds must be integers in 30..3600 and stale must cover refresh")
    unified = profile == "lockbot-v1"
    return {
        "id": "",
        "name": text(source.get("name", "Prometheus"), "name", 64),
        "type": "prometheus",
        "profile": "lockbot-v1" if unified else "custom",
        "accessMode": mode,
        "endpoint": endpoint.rstrip("/"),
        "nodeLabel": "node" if unified else "instance",
        "deviceLabel": "device" if unified else "UUID",
        "utilizationMetric": "lockbot_accelerator_utilization_percent" if unified else "DCGM_FI_DEV_GPU_UTIL",
        "memoryUsedMetric": "lockbot_accelerator_memory_used_bytes" if unified else "DCGM_FI_DEV_FB_USED",
        "memoryTotalMetric": "lockbot_accelerator_memory_total_bytes" if unified else "DCGM_FI_DEV_FB_TOTAL",
        "memoryUnit": "B" if unified else "MiB",
        "nodeMapping": nodes,
        "deviceMapping": devices,
        "intervalSeconds": interval,
        "staleSeconds": stale,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 128 * 1024:
            raise ValueError("Input exceeds 128 KiB")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON keys are not allowed")
                result[key] = value
            return result

        config = generate(json.loads(args.input.read_text(), object_pairs_hook=unique))
        out = args.output_dir
        if out.exists() and any(out.iterdir()):
            raise ValueError("Output directory must be empty; preserve existing handoff files")
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, data in [("utilization-config.json", config), ("node-mapping.json", config["nodeMapping"])]:
            path = out / name
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            path.chmod(0o600)
        lines = [
            "# 数据源填写表",
            "",
            "状态：仅生成，未联网验证、部署或保存。",
            "",
            "整份配置用于程序维护/审核；控制台没有整份 JSON 导入。仅 node-mapping.json 可复制到高级节点映射。",
            "",
            "| 字段 | 值 |",
            "| --- | --- |",
        ]
        for key in (
            "name",
            "type",
            "profile",
            "endpoint",
            "accessMode",
            "nodeLabel",
            "deviceLabel",
            "utilizationMetric",
            "memoryUsedMetric",
            "memoryTotalMetric",
            "memoryUnit",
            "intervalSeconds",
            "staleSeconds",
        ):
            value = str(config[key]).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"| {key} | {value} |")
        lines += ["", "## 节点/设备映射", ""]
        for name in ("nodeMapping", "deviceMapping"):
            lines.extend(
                [f"### {name}", "", "```json", json.dumps(config[name], ensure_ascii=False, indent=2), "```", ""]
            )
        lines += [
            "授权说明：独立认证需私下提供源专用凭证；自动短期授权需管理员事先建立签名信任。本输出不包含秘密，也不建立信任。",
            "",
            "网络说明：HTTP 未加密；Agent 可达不证明浏览器 CORS 或所有用户可达。真实目录可能敏感，不默认提交公共仓库。",
            "",
        ]
        path = out / "handoff.md"
        path.write_text("\n".join(lines))
        path.chmod(0o600)
        print("Generated utilization-config.json, node-mapping.json and handoff.md; no network or service changes.")
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"Configuration rejected: {error}\n")


if __name__ == "__main__":
    main()

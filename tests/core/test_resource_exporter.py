"""Exporter acquisition is independent of Prometheus scraping and Lockbot scheduling."""

import json
import subprocess
from concurrent.futures import Future
from unittest.mock import patch

import pytest
from lockbot.metrics.exporter import Collector, container_count, host_memory, parse_xml, validate_config

XML = """<nvidia_smi_log>
<gpu><minor_number>2</minor_number><utilization><gpu_util>70 %</gpu_util></utilization>
<fb_memory_usage><used>2048 MiB</used><total>8192 MiB</total></fb_memory_usage>
<processes><process_info><pid>11</pid></process_info><process_info><pid>12</pid></process_info>
<process_info><pid>11</pid></process_info></processes></gpu>
<gpu><minor_number>0</minor_number><utilization><gpu_util>N/A</gpu_util></utilization>
<fb_memory_usage><used>N/A</used><total>8192 MiB</total></fb_memory_usage><processes/></gpu>
</nvidia_smi_log>"""


def config(**overrides):
    return {"node": "node-a", "vendor": "nvidia", "command": ["nvidia-smi", "-q", "-x"], **overrides}


def test_xml_preserves_explicit_card_index_unknown_values_and_all_processes():
    cards = parse_xml(XML)
    assert [card.device for card in cards] == ["0", "2"]
    assert cards[0].utilization is None and cards[0].memory_used is None
    assert cards[0].processes == ()
    assert cards[1].processes == (11, 12)
    assert cards[1].memory_used == 2 * 1024**3
    assert cards[1].utilization == 70


@pytest.mark.parametrize(
    "xml",
    [
        "<log/>",
        "<log><gpu><minor_number>0</minor_number></gpu><gpu><minor_number>0</minor_number></gpu></log>",
        "<log><gpu><minor_number>01</minor_number></gpu></log>",
        '<!DOCTYPE log [<!ENTITY a "x">]><log/>',
    ],
)
def test_invalid_or_ambiguous_xml_fails_closed(xml):
    with pytest.raises(ValueError):
        parse_xml(xml)


def test_multiple_containers_are_counted_without_names_or_commandline_access(tmp_path):
    for pid, digest in [(11, "a" * 64), (12, "b" * 64), (13, "a" * 64)]:
        path = tmp_path / str(pid)
        path.mkdir()
        (path / "cgroup").write_text(f"0::/system.slice/docker-{digest}.scope")
    assert container_count((11, 12, 13), tmp_path) == 2
    assert container_count((11, 99), tmp_path) is None
    assert container_count(None, tmp_path) is None
    assert container_count((), tmp_path) == 0


def test_scrapes_only_read_cache_and_failures_do_not_forge_freshness(tmp_path):
    collector = Collector(config(proc_root=str(tmp_path)), clock=lambda: 1000, monotonic=lambda: 1)
    with patch("lockbot.metrics.exporter.run_xml", return_value=XML) as run:
        collector.collect_accelerator()
        bodies = [collector.render() for _ in range(100)]
        assert run.call_count == 1 and len(set(bodies)) == 1
    with patch("lockbot.metrics.exporter.run_xml", side_effect=ValueError):
        collector.clock = lambda: 2000
        collector.collect_accelerator()
    body = collector.render().decode()
    assert 'lockbot_sample_timestamp_seconds{node="node-a",collector="accelerator"} 1000' in body
    assert 'lockbot_collector_success{node="node-a",collector="accelerator"} 0' in body
    assert "process_info" not in body and "pid" not in body
    assert all(len(line.rsplit(" ", 1)) == 2 for line in body.splitlines())


def test_hardware_inventory_keeps_network_facts_in_the_private_export(tmp_path):
    output = tmp_path / "hardware.json"
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
    commands = {
        "hostname": "node.example.test\n",
        "ip": json.dumps(
            [
                {
                    "ifname": "eth0",
                    "addr_info": [
                        {"local": "192.0.2.19", "prefixlen": 24, "scope": "global"},
                        {"local": "2001:db8::19", "prefixlen": 64, "scope": "global"},
                    ],
                }
            ]
        ),
        "lscpu": json.dumps({"lscpu": [{"field": "CPU(s):", "data": "192"}]}),
    }
    collector = Collector(
        config(
            proc_root=str(tmp_path),
            inventory_output=str(output),
            inventory_scope="container",
        ),
        clock=lambda: 1000,
        monotonic=lambda: 1,
    )
    xml = XML.replace(
        "<minor_number>2</minor_number>",
        "<minor_number>2</minor_number><product_name>P900</product_name>"
        "<uuid>XPU-fixture</uuid><pci><pci_bus_id>00000000:1A:03.0</pci_bus_id></pci>",
    )
    with (
        patch("lockbot.metrics.exporter.run_xml", return_value=xml),
        patch("lockbot.metrics.exporter._hardware_command", side_effect=lambda command, *_: commands[command]),
        patch("lockbot.metrics.exporter.socket.gethostname", return_value="node"),
    ):
        collector.collect_accelerator()
    exported = json.loads(output.read_text())
    assert exported["scope"] == "container" and exported["cpu"]["logical_cpus"] == 192
    assert exported["network"]["hostname"] == "node" and exported["network"]["fqdn"] == "node.example.test"
    assert {item["ip"] for item in exported["network"]["addresses"]} == {"192.0.2.19", "2001:db8::19"}
    assert exported["devices"][1]["model"] == "P900" and exported["devices"][1]["memory_total_bytes"] == 8 * 1024**3
    assert exported["devices"][1]["device_uuid"] == "XPU-fixture"
    assert exported["devices"][1]["pci_bus"] == "0000:1a:03.0"
    assert exported["devices"][0]["device_uuid"] is None
    assert output.stat().st_mode & 0o777 == 0o600
    assert b"node.example.test" not in collector.render() and b"192.0.2.19" not in collector.render()
    assert b"XPU-fixture" not in collector.render() and b"0000:1a:03.0" not in collector.render()


def test_proc_memory_uses_available_not_just_free(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 10 kB\n")
    assert host_memory(tmp_path) == (400 * 1024, 1000 * 1024)


def test_host_cpu_uses_deltas_and_excludes_double_counted_guest(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
    (tmp_path / "stat").write_text("cpu 100 0 100 800 0 0 0 0 100 0\n")
    collector = Collector(config(proc_root=str(tmp_path)))
    collector.collect_host()
    assert b"lockbot_host_cpu_utilization_percent" not in collector.render()
    (tmp_path / "stat").write_text("cpu 150 0 100 850 0 0 0 0 150 0\n")
    collector.collect_host()
    assert b'lockbot_host_cpu_utilization_percent{node="node-a"} 50' in collector.render()


@pytest.mark.parametrize(
    "overrides",
    [
        {"interval_seconds": 1},
        {"host_interval_seconds": 0},
        {"timeout_seconds": 60},
        {"filesystems": {"secret/path": "/"}},
        {"command": "xpu-smi -q -x"},
    ],
)
def test_unsafe_or_excessive_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        validate_config(config(**overrides))
    json.dumps(config())


def test_command_summary_drops_secrets_and_arbitrary_arguments():
    from lockbot.metrics.exporter import command_summary

    text = command_summary(
        b"/usr/bin/python3\0/private/run_test.py\0--token\0super-secret-value\0--model\0/private/model\0"
    )
    assert text == "python3 run_test.py [arguments omitted]"
    assert "super-secret" not in text and "/private" not in text
    assert command_summary(b"python3\0-c\0print('secret')\0") == "python3 [arguments omitted]"


def test_fleet_probe_emits_normalized_all_card_data_without_raw_process_ids(tmp_path):
    from lockbot.metrics.exporter import probe

    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 600 kB\n")
    (tmp_path / "stat").write_text("cpu 100 0 100 800 0 0 0 0 100 0\n")
    with patch("lockbot.metrics.exporter.run_xml", return_value=XML):
        value = probe(config(proc_root=str(tmp_path), probe_lock=str(tmp_path / "probe.lock")))
    assert value["accelerator_success"] and value["host_success"]
    assert [item["device"] for item in value["cards"]] == ["0", "2"]
    assert value["cards"][1]["process_count"] == 2
    assert value["cards"][1]["process_info_truncated"] is True
    assert {"processes", "uuid", "pci_bus"}.isdisjoint(value["cards"][1])


def test_fleet_failure_keeps_old_source_time_and_queries_only_explicit_target(tmp_path):
    from lockbot.metrics.exporter import FleetCollector

    fleet = FleetCollector({"targets": [config(ssh_target="monitor@node-a.example.invalid")]})
    payload = {
        "host_success": False,
        "accelerator_success": True,
        "accelerator_observed_at": 1234,
        "accelerator_duration": 0.03,
        "cards": [{"device": "0", "utilization": 12, "memory_used": 4, "memory_total": 8, "process_info": []}],
    }
    with patch("lockbot.metrics.exporter.run_xml", return_value=json.dumps(payload)) as command:
        fleet.collect(fleet.targets[0])
        actual = command.call_args.args[0]
        assert "monitor@node-a.example.invalid" in actual
        assert "StrictHostKeyChecking=accept-new" in actual
        assert "StrictHostKeyChecking=no" not in actual
        for _ in range(100):
            fleet.render()
        assert command.call_count == 1
    with patch("lockbot.metrics.exporter.run_xml", side_effect=ValueError):
        fleet.collect(fleet.targets[0])
    body = fleet.render().decode()
    assert 'lockbot_sample_timestamp_seconds{node="node-a",collector="accelerator"} 1234' in body
    assert 'lockbot_collector_success{node="node-a",collector="accelerator"} 0' in body


def test_fleet_preserves_host_sample_and_inventory_when_accelerator_fails(tmp_path):
    from lockbot.metrics.exporter import FleetCollector

    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"devices":[{"device":"0","model":"P800"}]}')
    target = config(ssh_target="monitor@node-a.example.invalid", inventory_output=str(inventory))
    fleet = FleetCollector({"targets": [target]})
    with patch(
        "lockbot.metrics.exporter.run_xml",
        return_value=json.dumps(
            {
                "host_success": True,
                "host_observed_at": 1234,
                "cpu": [100, 20],
                "memory": [30, 100],
                "filesystems": {},
                "accelerator_success": True,
                "accelerator_observed_at": 1234,
                "cards": None,
                "hardware_inventory": {"devices": []},
            }
        ),
    ):
        assert fleet.collect(target)
    body = fleet.render().decode()
    assert 'lockbot_host_memory_used_bytes{node="node-a"} 30' in body
    assert 'lockbot_collector_success{node="node-a",collector="accelerator"} 0' in body
    assert json.loads(inventory.read_text())["devices"][0]["model"] == "P800"


def test_fleet_backs_off_failed_nodes_without_delaying_healthy_nodes(monkeypatch):
    from lockbot.metrics import exporter

    class Clock:
        now = 0

        def is_set(self):
            return self.now > 540

        def wait(self, seconds):
            self.now += 30

    class Executor:
        def __init__(self, *, max_workers):
            assert max_workers == 1

        def __enter__(self):
            return self

        def __exit__(self, *error):
            return False

        def submit(self, collect, target):
            future = Future()
            try:
                future.set_result(collect(target))
            except Exception as error:
                future.set_exception(error)
            return future

    clock = Clock()
    offline = config(ssh_target="monitor@offline.example.invalid")
    healthy = config(node="node-b", ssh_target="monitor@healthy.example.invalid")
    fleet = exporter.FleetCollector({"targets": [offline, healthy], "interval_seconds": 60, "max_workers": 1})
    calls = {"node-a": [], "node-b": []}

    def collect(target):
        node = target["node"]
        calls[node].append(clock.now)
        if node == "node-a" and clock.now < 420:
            raise subprocess.TimeoutExpired("ssh", 25)
        return True

    monkeypatch.setattr(fleet, "collect", collect)
    monkeypatch.setattr(exporter.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(exporter.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(exporter, "ThreadPoolExecutor", Executor)
    fleet.run(clock)
    assert calls["node-a"] == [0, 150, 420, 510]
    assert calls["node-b"] == [30, 120, 210, 300, 390, 480]


def test_xpu_xml_uses_identical_metric_units_and_explicit_indices():
    cards = parse_xml(
        XML.replace("nvidia_smi_log", "xpu_smi_log").replace("gpu>", "xpu>").replace("gpu_util", "xpu_util")
    )
    assert [card.device for card in cards] == ["0", "2"]
    assert cards[1].memory_used == 2 * 1024**3 and cards[1].utilization == 70


def test_epoch_and_large_byte_metrics_preserve_round_trip_precision():
    from lockbot.metrics.exporter import sample

    epoch = 1793243671.123456
    timestamp_line = sample("lockbot_sample_timestamp_seconds", epoch, {"node": "node-a"})
    assert float(timestamp_line.rsplit(" ", 1)[1]) == epoch
    used_bytes = 1234567890123
    memory_line = sample("lockbot_host_memory_used_bytes", used_bytes, {"node": "node-a"})
    assert int(memory_line.rsplit(" ", 1)[1]) == used_bytes
    fractional = 1234567890123.125
    assert float(sample("metric", fractional, {}).rsplit(" ", 1)[1]) == fractional


def test_memory_cache_and_available_are_distinct_and_missing_is_unknown(tmp_path):
    from lockbot.metrics.exporter import host_memory_details

    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 1000 kB\nMemAvailable: 200 kB\nBuffers: 10 kB\nCached: 500 kB\nSReclaimable: 30 kB\n")
    assert host_memory_details(tmp_path) == {"available_bytes": 200 * 1024, "cache_bytes": 540 * 1024}
    path.write_text("MemTotal: 1000 kB\nMemAvailable: 200 kB\n")
    assert host_memory_details(tmp_path)["cache_bytes"] is None

"""Exporter acquisition is independent of Prometheus scraping and Lockbot scheduling."""

import json
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
    assert "processes" not in value["cards"][1]


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

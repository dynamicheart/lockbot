"""Structured observed facts only: no driver, SSH, socket or management call."""

import copy
import dataclasses
import json
from unittest.mock import Mock
from xml.sax.saxutils import escape

import pytest
from lockbot.metrics import exporter


def record(device="0", *, layout="gpu", model="P800", uuid="GPU-observed-0000", extra=""):
    index = {"gpu": "minor_number", "xpu": "device_id", "device": "index"}[layout]
    fields = ""
    if model is not None:
        fields += "<product_name>" + escape(model) + "</product_name>"
    if uuid is not None:
        fields += "<uuid>" + escape(uuid) + "</uuid>"
    return (
        f"<{layout}><{index}>{device}</{index}>{fields}"
        "<utilization><gpu_util>0 %</gpu_util><xpu_util>0 %</xpu_util></utilization>"
        "<fb_memory_usage><used>4 MiB</used><total>16 MiB</total></fb_memory_usage>"
        f"{extra}</{layout}>"
    )


def xml(*records):
    return "<observed>" + "".join(records) + "</observed>"


def config(tmp_path, **changes):
    (tmp_path / "stat").write_text("cpu 1 0 1 8 0 0 0 0\n")
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    return {
        "node": "configured-node",
        "vendor": "xpu",
        "command": ["approved-fixture-command"],
        "proc_root": str(tmp_path),
        "probe_lock": str(tmp_path / "probe.lock"),
        **changes,
    }


def test_card_existing_five_positional_arguments_remain_compatible():
    card = exporter.Card("0", 0, 1, 2, ())
    assert card.model is None and card.uuid is None
    assert (card.device, card.utilization, card.memory_used, card.memory_total, card.processes) == ("0", 0, 1, 2, ())


@pytest.mark.parametrize("layout", ["gpu", "xpu", "device"])
def test_explicit_recognized_layout_retains_fields_without_changing_index(layout):
    cards = exporter.parse_xml(
        xml(
            record("2", layout=layout, model="Vendor P900", uuid="observed-card-2"),
            record("0", layout=layout, model="Vendor P800", uuid="observed-card-0"),
        )
    )
    assert [card.device for card in cards] == ["0", "2"]
    assert [(card.model, card.uuid) for card in cards] == [
        ("Vendor P800", "observed-card-0"),
        ("Vendor P900", "observed-card-2"),
    ]
    assert cards[0].utilization == 0 and cards[0].memory_used == 4 * 1024**2


def test_unknown_fields_are_not_inferred_from_index_config_or_aliases():
    (card,) = exporter.parse_xml(
        xml(record(model=None, uuid=None, extra="<model>P800</model><name>P900</name><serial>guessed-id</serial>"))
    )
    assert card.device == "0" and card.model is None and card.uuid is None
    observation = exporter.device_fact_observation([card], 1000)
    assert observation["coverage"] == "partial"
    assert observation["cards"] == [{"device": "0", "model": None, "uuid": None}]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "N/A"),
        ("model", "[Not Supported]"),
        ("model", "unknown"),
        ("model", "m" * 129),
        ("model", "界" * 43),
        ("model", "line\nbreak"),
        ("model", "hidden\u200bformat"),
        ("model", "lbr_" + "s" * 24),
        ("model", "ｌｂｒ＿" + "Ａ" * 20),
        ("uuid", "0"),
        ("uuid", "GPU-00000000-0000-0000-0000-000000000000"),
        ("uuid", "N/A"),
        ("uuid", "not supported"),
        ("uuid", "space is invalid"),
        ("uuid", "u" * 129),
        ("uuid", "GPU-汉字"),
        ("uuid", "lbk_" + "s" * 24),
    ],
)
def test_malformed_or_secret_optional_fields_become_unknown_without_losing_utilization(field, value):
    kwargs = {"model": "P800", "uuid": "GPU-observed-0000", field: value}
    (card,) = exporter.parse_xml(xml(record(**kwargs)))
    assert getattr(card, field) is None
    assert card.utilization == 0 and card.memory_total == 16 * 1024**2
    observation = exporter.device_fact_observation([card], 1000)
    assert observation["coverage"] == "partial"
    if len(value) > 3:
        assert value not in json.dumps(observation, ensure_ascii=False)


def test_duplicate_direct_fields_are_unknown_even_if_equal():
    (card,) = exporter.parse_xml(xml(record(extra="<product_name>P800</product_name><uuid>GPU-observed-0000</uuid>")))
    assert card.model is None and card.uuid is None and card.utilization == 0


def test_nested_fields_and_mixed_layout_facts_are_not_guessed():
    nested = (
        "<gpu><minor_number>0</minor_number><product_name><name>P800</name></product_name><uuid><id>x</id></uuid></gpu>"
    )
    (card,) = exporter.parse_xml(xml(nested))
    assert card.model is None and card.uuid is None
    cards = exporter.parse_xml(xml(record(layout="gpu"), record("1", layout="xpu")))
    # Preserve the old utilization-layout choice, without claiming fact coverage.
    assert [card.device for card in cards] == ["0"]
    assert cards[0].model is None and cards[0].uuid is None
    assert exporter.device_fact_observation(cards, 1000)["coverage"] == "partial"


def test_duplicate_uuid_clears_only_affected_identity_fields():
    cards = exporter.parse_xml(
        xml(
            record("0", uuid="GPU-shared-identity"),
            record("1", uuid="gpu-shared-identity"),
            record("2", uuid="GPU-independent-identity"),
        )
    )
    assert [card.uuid for card in cards] == [None, None, "GPU-independent-identity"]
    assert all(card.model == "P800" and card.utilization == 0 for card in cards)
    assert exporter.device_fact_observation(cards, 1000)["coverage"] == "partial"


def test_observation_provenance_coverage_and_bounds_exclude_xml_processes_and_names(monkeypatch):
    card = exporter.Card("0", 0, 1, 2, (987654,), "P800", "GPU-observed-identity")
    observation = exporter.device_fact_observation([card], 1234.125)
    assert observation == {
        "schema_version": 1,
        "source": "structured_xml",
        "observed_at": 1234.125,
        "coverage": "complete",
        "cards": [{"device": "0", "model": "P800", "uuid": "GPU-observed-identity"}],
        "unidentified_devices": 0,
        "truncated": False,
    }
    assert len(json.dumps(observation).encode()) <= exporter.MAX_DEVICE_FACTS_BYTES
    serialized = json.dumps(observation)
    assert "987654" not in serialized and "process" not in serialized and "<gpu" not in serialized
    too_many = exporter.device_fact_observation([card] * 257, 1234)
    assert too_many["coverage"] == "partial" and too_many["truncated"] and not too_many["cards"]
    monkeypatch.setattr(exporter, "MAX_DEVICE_FACTS_BYTES", 256)
    bounded = exporter.device_fact_observation(
        [
            dataclasses.replace(card, device=str(index), uuid="GPU-observed-identity-" + str(index))
            for index in range(3)
        ],
        1234,
    )
    assert bounded["truncated"] and bounded["coverage"] == "partial" and not bounded["cards"]


@pytest.mark.parametrize("observed_at", [None, float("nan"), float("inf"), -1, True, 10**1000, -(10**1000)])
def test_invalid_sample_time_never_forges_complete_facts(observed_at):
    observation = exporter.device_fact_observation(
        [exporter.Card("0", 0, 1, 2, (), "P800", "GPU-id-0000")], observed_at
    )
    assert observation["observed_at"] is None and observation["coverage"] == "partial"
    json.dumps(observation, allow_nan=False)


def test_duplicate_or_null_remote_device_ids_are_not_repaired_or_joined():
    observation = exporter.device_fact_observation(
        [
            {"device": "0", "model": "P800", "uuid": "GPU-one"},
            {"device": "0", "model": "P900", "uuid": "GPU-two"},
            {"device": None, "model": "P800", "uuid": "GPU-three"},
            {"device": "1", "model": "P900", "uuid": "GPU-four"},
        ],
        1000,
    )
    assert observation["coverage"] == "partial" and observation["unidentified_devices"] == 3
    assert observation["cards"] == [{"device": "1", "model": "P900", "uuid": "GPU-four"}]


def test_local_collector_retains_facts_without_new_metrics_or_probe_on_scrape(tmp_path, monkeypatch):
    settings = config(tmp_path)
    clock = [1000]
    acquired = xml(record(extra="<processes/>"))

    def run(command, timeout):
        assert command == settings["command"]
        clock[0] = 1010
        return acquired

    command = Mock(side_effect=run)
    monkeypatch.setattr(exporter, "run_xml", command)
    collector = exporter.Collector(settings, clock=lambda: clock[0], monotonic=lambda: 1)
    collector.collect_accelerator()
    observation = copy.deepcopy(collector.device_facts)
    assert observation["observed_at"] == 1010 and observation["coverage"] == "complete"
    bodies = [collector.render() for _ in range(100)]
    assert command.call_count == 1 and len(set(bodies)) == 1 and collector.device_facts == observation
    assert b"GPU-observed" not in bodies[0] and b"P800" not in bodies[0]
    assert b'device="0"' in bodies[0] and b"model=" not in bodies[0] and b"uuid=" not in bodies[0]
    # Optional facts must not change existing metric timestamps or values.
    assert b'collector="accelerator"} 1000' in bodies[0]
    monkeypatch.setattr(exporter, "run_xml", Mock(side_effect=ValueError("synthetic acquisition failure")))
    collector.collect_accelerator()
    assert collector.device_facts is None
    assert b'collector="accelerator"} 1000' in collector.render()
    assert b'lockbot_collector_success{node="configured-node",collector="accelerator"} 0' in collector.render()


def test_probe_retains_same_sample_facts_with_no_xml_or_process_identity_output(tmp_path, monkeypatch, capsys):
    settings = config(tmp_path)
    sample_clock = [1000]
    raw = xml(
        record(
            "2",
            extra="<processes><process_info><pid>987654</pid><process_name>private-name</process_name></process_info></processes>",
        )
    )

    def run(command, timeout):
        sample_clock[0] = 1234.5
        return raw

    monkeypatch.setattr(exporter, "run_xml", Mock(side_effect=run))
    monkeypatch.setattr(exporter, "process_metadata", lambda cards, root: {})
    monkeypatch.setattr(exporter.time, "time", lambda: sample_clock[0])
    payload = exporter.probe(settings)
    assert payload["accelerator_success"]
    assert payload["accelerator_observed_at"] == payload["device_facts"]["observed_at"] == 1234.5
    assert payload["cards"][0]["device"] == "2"
    assert payload["cards"][0]["model"] == "P800" and payload["cards"][0]["uuid"] == "GPU-observed-0000"
    assert payload["device_facts"]["source"] == "structured_xml"
    serialized = json.dumps(payload["device_facts"])
    assert "987654" not in serialized and "private-name" not in serialized and "<process" not in serialized
    assert capsys.readouterr() == ("", "")


def test_fleet_binds_only_configured_target_and_old_probes_remain_partial(tmp_path, monkeypatch):
    target = config(tmp_path)
    payload = {
        "node": "untrusted-remote-node",
        "host_success": False,
        "accelerator_success": True,
        "accelerator_duration": 0.01,
        "accelerator_observed_at": 1200,
        "cards": [
            {
                "device": "0",
                "utilization": 0,
                "memory_used": 1,
                "memory_total": 2,
                "model": "P900",
                "uuid": "GPU-observed-identity",
            }
        ],
    }
    monkeypatch.setattr(exporter, "probe", Mock(return_value=payload))
    fleet = exporter.FleetCollector({"targets": [target]})
    fleet.collect(target)
    assert set(fleet.device_facts) == {"configured-node"}
    assert fleet.device_facts["configured-node"]["coverage"] == "complete"
    first = fleet.render()
    assert b"GPU-observed" not in first and b"P900" not in first
    payload["cards"][0].pop("model")
    payload["cards"][0].pop("uuid")
    fleet.collect(target)
    assert fleet.device_facts["configured-node"]["coverage"] == "partial"
    assert fleet.render() == first
    monkeypatch.setattr(exporter, "probe", Mock(side_effect=ValueError("synthetic failure")))
    fleet.collect(target)
    assert fleet.device_facts["configured-node"] is None
    assert b'collector="accelerator"} 1200' in fleet.render()


def test_simulated_ssh_reuses_one_existing_target_call_and_cached_render(tmp_path, monkeypatch):
    target = config(tmp_path, ssh_target="monitor@approved.example.invalid")
    payload = {
        "host_success": False,
        "accelerator_success": True,
        "accelerator_duration": 0.01,
        "accelerator_observed_at": 1200,
        "cards": [{"device": "0", "utilization": 0, "model": "P800", "uuid": "GPU-observed-identity"}],
    }
    command = Mock(return_value=json.dumps(payload))
    monkeypatch.setattr(exporter, "run_xml", command)
    fleet = exporter.FleetCollector({"targets": [target]})
    fleet.collect(target)
    assert command.call_count == 1
    assert target["ssh_target"] in command.call_args.args[0]
    assert fleet.device_facts["configured-node"]["coverage"] == "complete"
    for _ in range(100):
        fleet.render()
    assert command.call_count == 1


def test_maximum_field_and_card_bounds_retain_complete_observed_facts():
    cards = [exporter.Card(str(index), 0, 1, 2, (), "M" * 128, f"{index:03}" + "a" * 125) for index in range(256)]
    observation = exporter.device_fact_observation(cards, 1000)
    assert observation["coverage"] == "complete" and len(observation["cards"]) == 256
    assert not observation["truncated"] and observation["unidentified_devices"] == 0
    assert len(json.dumps(observation, ensure_ascii=True).encode()) <= exporter.MAX_DEVICE_FACTS_BYTES


@pytest.mark.parametrize("dropped", ["invalid_index", "duplicate_index"])
def test_dropped_rows_still_contradict_retained_uuid_claims(dropped):
    rows = [
        {"device": "1", "model": "P800", "uuid": "GPU-shared-identity"},
        {"device": "3", "model": "P900", "uuid": "GPU-independent-identity"},
    ]
    if dropped == "invalid_index":
        rows.append({"device": None, "model": "P800", "uuid": "gpu-shared-identity"})
    else:
        rows.extend(
            [
                {"device": "0", "model": "P800", "uuid": "gpu-shared-identity"},
                {"device": "0", "model": "P900", "uuid": "GPU-other-dropped-identity"},
            ]
        )
    observation = exporter.device_fact_observation(rows, 1000)
    assert observation["coverage"] == "partial"
    assert observation["unidentified_devices"] == (1 if dropped == "invalid_index" else 2)
    assert observation["cards"] == [
        {"device": "1", "model": "P800", "uuid": None},
        {"device": "3", "model": "P900", "uuid": "GPU-independent-identity"},
    ]


def test_fleet_normalizes_optional_facts_before_publishing_any_new_sample(tmp_path, monkeypatch):
    target = config(tmp_path)
    payload = {
        "host_success": True,
        "host_observed_at": 1000,
        "host_duration": 0.01,
        "cpu": (100, 10),
        "memory": (1, 2),
        "filesystems": {},
        "accelerator_success": True,
        "accelerator_observed_at": 1000,
        "accelerator_duration": 0.01,
        "cards": [{"device": "0", "utilization": 0, "model": "P800", "uuid": "GPU-observed-identity"}],
    }
    monkeypatch.setattr(exporter, "probe", Mock(return_value=payload))
    fleet = exporter.FleetCollector({"targets": [target]})
    fleet.collect(target)
    bodies, statuses, cpu = copy.deepcopy(fleet.bodies), copy.deepcopy(fleet.statuses), dict(fleet.previous_cpu)
    payload.update(host_observed_at=2000, accelerator_observed_at=2000, cpu=(200, 20), memory=(2, 4))
    payload["cards"][0]["utilization"] = 99

    def failed_facts(cards, observed_at):
        assert fleet.bodies == bodies and fleet.statuses == statuses
        assert fleet.previous_cpu == cpu
        raise ValueError("synthetic fact normalization failure")

    monkeypatch.setattr(exporter, "device_fact_observation", failed_facts)
    fleet.collect(target)
    assert fleet.bodies == bodies and fleet.previous_cpu == cpu
    assert fleet.statuses["configured-node", "host"] == (0, 0, 1000)
    assert fleet.statuses["configured-node", "accelerator"] == (0, 0, 1000)
    assert fleet.device_facts["configured-node"] is None

"""Packaged local-label CLI acceptance; no service, provider or Key is used."""

import base64
import io
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import quote
from zipfile import ZipFile

import pytest
from lockbot.backend.app.resource_api import skill


def native_ref(node="same-node", scope="a"):
    return "s-" + scope * 64 + "/" + quote(node, safe="")


def external_ref(node="same-node", namespace="b", scope="c"):
    inner = "x-" + scope * 64 + "/" + quote(node, safe="")
    token = base64.urlsafe_b64encode(inner.encode()).decode().rstrip("=")
    return "e1." + namespace * 64 + "." + token


@pytest.fixture
def labels_cli(tmp_path):
    installed = tmp_path / "installed"
    with ZipFile(io.BytesIO(skill.installation_bundle())) as archive:
        archive.extractall(installed)
    script = installed / "lockbot-agent/scripts/lockbot_cli.py"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    state = private / "labels.json"
    native_home = tmp_path / "unused-native-home"
    marker = private / "forbidden-access"
    # Valid-looking credentials are synthetic sentinels, never query authority.
    key = private / "never-read-key"
    key.write_text("lbr_" + "z" * 32)
    key.chmod(0o600)
    connection = private / "forbidden-connection.json"
    connection.write_text("not a connection; local labels must not inspect it")
    connection.chmod(0o600)
    provider = private / "forbidden-provider.json"
    provider.write_text("not a registration; local labels must not inspect it")
    provider.chmod(0o600)
    sources = private / "forbidden-sources.json"
    sources.write_text("not a sources catalogue; local labels must not inspect it")
    sources.chmod(0o600)
    protected = [str(path) for path in (connection, provider, sources, key)]
    bootstrap = r'''import json, pathlib, runpy, sys, urllib.request
script, protected, marker, *args = sys.argv[1:]
protected = set(json.loads(protected))
namespace = runpy.run_path(script, run_name='local_label_acceptance')
main = namespace['main']
def forbidden(*args, **kwargs):
    pathlib.Path(marker).write_text('forbidden authority or network access')
    raise AssertionError('local labels accessed resource authority')
for name in ('Client', 'listed_connections', 'resources_all', 'resource_provider_output',
             'mixed_resource_list', '_mixed_sources'):
    if name in main.__globals__:
        main.__globals__[name] = forbidden
for name in ('private_bytes', 'private_json'):
    original = main.__globals__.get(name)
    if original is not None:
        def guarded(path, *args, _original=original, **kwargs):
            if str(pathlib.Path(path).absolute()) in protected:
                forbidden()
            return _original(path, *args, **kwargs)
        main.__globals__[name] = guarded
urllib.request.urlopen = forbidden
urllib.request.build_opener = forbidden
raise SystemExit(main(args))
'''

    def invoke(*arguments, code=0, guard=False, global_profiles=False, human=False, ascii_stdout=False):
        flags = []
        if global_profiles:
            flags = ["--connection", str(connection), "--resource-provider", str(provider),
                     "--resource-sources", str(sources)]
        arguments = [*flags, "resource", "label", *map(str, arguments)]
        command = [sys.executable, "-I", "-S", str(script), *arguments]
        if guard or ascii_stdout:
            prelude = "import sys; sys.stdout.reconfigure(encoding='ascii', errors='strict')\n" if ascii_stdout else ""
            command = [sys.executable, "-I", "-S", "-c", prelude + bootstrap, str(script),
                       json.dumps(protected), str(marker), *arguments]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["LOCKBOT_HOME"] = str(native_home)
        process = subprocess.run(command, env=environment, capture_output=True,
                                 text=True, timeout=8)
        assert process.returncode == code, process.stderr
        assert not marker.exists(), "Local metadata touched a resource source or Key"
        assert not native_home.exists(), "Local metadata initialized a resource catalogue"
        assert "lbr_" + "z" * 32 not in process.stdout + process.stderr
        if code:
            value = json.loads(process.stderr)
            assert not process.stdout
            assert value["retry_mutation"] is False
            return value
        assert len(process.stdout.encode()) <= 8192
        assert not process.stderr
        return process.stdout if human else json.loads(process.stdout)

    return SimpleNamespace(invoke=invoke, state=state, private=private, marker=marker,
                           connection=connection, provider=provider, sources=sources)


def result(value):
    data = value["result"]
    assert data["scope"] == "local_private_metadata" and data["local_only"] is True
    assert isinstance(data["rows"], list)
    assert all(isinstance(row.get("ref"), str) for row in data["rows"])
    return data


def write_label(cli, ref, label):
    return result(cli.invoke("add", ref, label, "--file", cli.state, "--json", "ref,labels"))


def test_missing_reads_and_field_discovery_never_create_state(labels_cli):
    cli = labels_cli
    default = result(cli.invoke("list", "--json", "ref,labels"))
    assert default["rows"] == []
    page = result(cli.invoke("list", "--file", cli.state, "--json", "ref,labels"))
    assert page["rows"] == [] and not page["has_more"] and page["next_cursor"] is None
    one = result(cli.invoke("view", native_ref(), "--file", cli.state, "--json", "ref,labels"))
    assert one["rows"] == [{"ref": native_ref(), "labels": []}]
    for args in [("list",), ("view", native_ref()), ("add", native_ref(), "unwritten")]:
        fields = cli.invoke(*args, "--file", cli.state, "--json", guard=True, global_profiles=True)
        assert set(fields["json_fields"]) == {"ref", "labels"}
        assert fields["required_row_fields"] == ["ref"]
    assert not cli.state.exists()


def test_add_view_list_remove_clear_are_private_local_and_idempotent(labels_cli):
    cli, ref = labels_cli, native_ref("未知节点 001")
    added = result(cli.invoke("add", ref, "Ｐ８００", "--file", cli.state, "--json", "ref,labels",
                              guard=True, global_profiles=True))
    assert added["changed"] is True and added["rows"] == [{"ref": ref, "labels": ["Ｐ８００"]}]
    repeated = write_label(cli, ref, "p800")
    assert repeated["changed"] is False and repeated["rows"] == added["rows"]
    write_label(cli, ref, "训练")
    one = result(cli.invoke("view", ref, "--file", cli.state, "--json", "labels"))
    assert one["rows"][0]["ref"] == ref and set(one["rows"][0]["labels"]) == {"Ｐ８００", "训练"}
    before_selection = cli.state.read_bytes()
    refs = result(cli.invoke("list", "--file", cli.state, "--json", "ref"))
    assert refs["rows"] == [{"ref": ref}] and cli.state.read_bytes() == before_selection
    removed = result(cli.invoke("remove", ref, "P800", "--file", cli.state, "--json", "ref,labels"))
    assert removed["changed"] is True and removed["rows"] == [{"ref": ref, "labels": ["训练"]}]
    repeated = result(cli.invoke("remove", ref, "P800", "--file", cli.state, "--json", "ref,labels"))
    assert repeated["changed"] is False
    cleared = result(cli.invoke("clear", ref, "--file", cli.state, "--json", "ref,labels"))
    assert cleared["changed"] is True and cleared["rows"] == [{"ref": ref, "labels": []}]
    assert result(cli.invoke("clear", ref, "--file", cli.state, "--json", "ref,labels"))["changed"] is False
    assert stat.S_IMODE(cli.state.stat().st_mode) == 0o600
    assert json.loads(cli.state.read_text())["schema_version"] == 1


def test_default_text_mutation_and_view_retain_full_reference_and_display_label(labels_cli):
    cli, ref = labels_cli, external_ref("同名机器")
    text = cli.invoke("add", ref, "临时", "--file", cli.state, human=True)
    assert ref in text and "临时" in text
    text = cli.invoke("view", ref, "--file", cli.state, human=True)
    assert ref in text and "临时" in text


def test_equal_names_and_external_namespaces_remain_separate_exact_refs(labels_cli):
    cli = labels_cli
    refs = [native_ref(), native_ref(scope="d"), external_ref(), external_ref(namespace="e")]
    for index, ref in enumerate(refs):
        write_label(cli, ref, "tag-" + str(index))
    rows = result(cli.invoke("list", "--file", cli.state, "--json", "labels"))["rows"]
    assert {row["ref"]: row["labels"] for row in rows} == {
        ref: ["tag-" + str(index)] for index, ref in enumerate(refs)
    }


def test_exact_label_filter_uses_nfkc_casefold_and_does_not_fuzzy_match(labels_cli):
    cli = labels_cli
    write_label(cli, native_ref("one"), "Straße")
    write_label(cli, native_ref("two"), "Strassen")
    write_label(cli, native_ref("three"), "Ｐ８００")
    page = result(cli.invoke("list", "--label", "STRASSE", "--file", cli.state, "--json", "ref,labels"))
    assert page["rows"] == [{"ref": native_ref("one"), "labels": ["Straße"]}]
    page = result(cli.invoke("list", "--label", "p800", "--file", cli.state, "--json", "ref"))
    assert page["rows"] == [{"ref": native_ref("three")}]


def test_default_limit_paging_and_edits_invalidate_content_bound_cursor(labels_cli):
    cli = labels_cli
    for number in range(23):
        write_label(cli, native_ref(f"node-{number:02}"), "group")
    first = result(cli.invoke("list", "--file", cli.state, "--json", "ref,labels"))
    assert len(first["rows"]) == 20 and first["has_more"] and first["next_cursor"]
    second = result(cli.invoke("list", "--file", cli.state, "--json", "ref,labels",
                               "--cursor", first["next_cursor"]))
    rows = first["rows"] + second["rows"]
    assert len(rows) == len({row["ref"] for row in rows}) == 23
    assert not second["has_more"] and second["next_cursor"] is None
    assert [row["ref"] for row in rows] == sorted(row["ref"] for row in rows)
    write_label(cli, native_ref("later"), "group")
    error = cli.invoke("list", "--file", cli.state, "--json", "ref,labels",
                       "--cursor", first["next_cursor"], code=2)
    assert "cursor" in error["error"]


@pytest.mark.parametrize("changed", ["fields", "limit", "filter"])
def test_list_cursor_binds_fields_limit_and_normalized_filter(labels_cli, changed):
    cli = labels_cli
    for number in range(3):
        write_label(cli, native_ref(str(number)), "Ｐ８００")
    first = result(cli.invoke("list", "--file", cli.state, "--label", "p800", "--limit", "1",
                              "--json", "labels"))
    args = ["list", "--file", cli.state, "--label", "Ｐ８００", "--limit", "1", "--json", "labels",
            "--cursor", first["next_cursor"]]
    # Equivalent normalization must resume successfully before testing a change.
    assert len(result(cli.invoke(*args))["rows"]) == 1
    if changed == "fields":
        args[args.index("labels")] = "ref"
    elif changed == "limit":
        args[args.index("1")] = "2"
    else:
        args[args.index("Ｐ８００")] = "other"
    assert "cursor" in cli.invoke(*args, code=2)["error"]


@pytest.mark.parametrize("ascii_stdout", [False, True])
def test_bounded_json_pages_never_trim_or_skip_oversized_resource_rows(labels_cli, ascii_stdout):
    cli = labels_cli
    refs = [external_ref(str(number) + "🙂" * 210) for number in range(4)]
    for ref in refs:
        for number in range(8):
            write_label(cli, ref, str(number) + "界" * 26 + "x")
    rows, cursor = [], None
    for _ in range(6):
        args = ["list", "--file", cli.state, "--limit", "256", "--json", "labels"]
        if cursor:
            args += ["--cursor", cursor]
        page = result(cli.invoke(*args, ascii_stdout=ascii_stdout))
        rows.extend(page["rows"])
        assert all(len(row["labels"]) == 8 for row in page["rows"])
        assert all(len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) >= 4096
                   for row in page["rows"])
        previous, cursor = cursor, page["next_cursor"]
        if cursor:
            assert cursor.startswith("lc1.")
            payload = cursor.split(".", 1)[1]
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            decoded = json.loads(raw)
            assert decoded[0] == 1 and len(decoded) == 4
            assert all(isinstance(value, str) and len(value) == 64 for value in decoded[1:])
            assert str(cli.state).encode() not in raw
            assert all(ref.encode() not in raw for ref in refs)
            assert all(label.encode() not in raw for row in page["rows"] for label in row["labels"])
        assert not cursor or cursor != previous
        if cursor is None:
            break
    assert cursor is None and {row["ref"] for row in rows} == set(refs) and len(rows) == 4


@pytest.mark.parametrize("ref", ["9", "node-a", "x-" + "a" * 64 + "/node-a",
                                  "s-" + "a" * 12 + "/node-a",
                                  "s-" + "a" * 64 + "/node%2fa",
                                  "e1." + "a" * 12 + ".eA", "lbr_" + "q" * 32,
                                  external_ref("lbk_" + "s" * 32)])
def test_invalid_or_ambiguous_references_reject_before_files_or_authority(labels_cli, ref):
    cli = labels_cli
    error = cli.invoke("add", ref, "tag", "--file", cli.state, "--json", "ref,labels",
                       guard=True, global_profiles=True, code=2)
    assert error["error"] and not cli.state.exists()
    assert ref not in json.dumps(error)


@pytest.mark.parametrize("label", ["", " leading", "trailing ", "line\nbreak", "x" * 81,
                                    "é" * 41, "lbr_" + "q" * 32, "lbk_" + "r" * 32, "ｌｂｒ＿" + "Ａ" * 20])
def test_invalid_or_sensitive_labels_are_not_echoed_or_written(labels_cli, label):
    cli = labels_cli
    error = cli.invoke("add", native_ref(), label, "--file", cli.state,
                       "--json", "ref,labels", code=2, guard=True)
    assert not cli.state.exists()
    if label:
        assert label not in json.dumps(error)


def test_eight_label_limit_preserves_existing_state_on_rejection(labels_cli):
    cli, ref = labels_cli, native_ref()
    for number in range(8):
        write_label(cli, ref, "label-" + str(number))
    before = cli.state.read_bytes()
    error = cli.invoke("add", ref, "ninth", "--file", cli.state, "--json", "ref,labels", code=2)
    assert error["error"] and cli.state.read_bytes() == before


def test_private_state_rejects_final_symlink_and_insecure_file_without_changes(labels_cli):
    cli = labels_cli
    write_label(cli, native_ref(), "tag")
    before = cli.state.read_bytes()
    link = cli.private / "linked.json"
    link.symlink_to(cli.state)
    assert cli.invoke("clear", native_ref(), "--file", link, "--json", "ref,labels", code=2)["error"]
    assert cli.state.read_bytes() == before
    cli.state.chmod(0o644)
    assert cli.invoke("view", native_ref(), "--file", cli.state, "--json", "ref,labels", code=2)["error"]
    assert cli.state.read_bytes() == before


def test_oversized_state_rejects_without_echoing_content_or_rewriting(labels_cli):
    cli = labels_cli
    cli.state.write_bytes(b"sensitive-private-metadata" + b"x" * (1024 * 1024))
    cli.state.chmod(0o600)
    before = cli.state.read_bytes()
    error = cli.invoke("list", "--file", cli.state, "--json", "ref,labels", code=2)
    assert "sensitive-private-metadata" not in json.dumps(error)
    assert cli.state.read_bytes() == before


def test_bare_mutation_json_discovers_fields_without_source_or_file_access(labels_cli):
    cli = labels_cli
    absent = cli.private / "uncreated-parent" / "labels.json"
    value = cli.invoke("add", external_ref(), "unwritten", "--file", absent, "--json",
                       guard=True, global_profiles=True)
    assert value == {"json_fields": ["ref", "labels"], "required_row_fields": ["ref"]}
    assert not absent.parent.exists() and not cli.state.exists()


def test_1024_reference_limit_preserves_existing_metadata(labels_cli):
    cli = labels_cli
    seed = native_ref()
    write_label(cli, seed, "tag")
    data = json.loads(cli.state.read_text())
    entry = data["resources"][seed]
    data["resources"] = {native_ref(f"node-{number:04}"): entry for number in range(1024)}
    cli.state.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    before = cli.state.read_bytes()
    assert len(before) < 1024 * 1024
    error = cli.invoke("add", native_ref("overflow"), "tag", "--file", cli.state,
                       "--json", "ref,labels", code=2)
    assert error["error"] and cli.state.read_bytes() == before


def test_changed_cursor_query_rejects_before_parsing_corrupted_metadata(labels_cli):
    cli = labels_cli
    for number in range(2):
        write_label(cli, native_ref(str(number)), "tag")
    first = result(cli.invoke("list", "--file", cli.state, "--limit", "1", "--json", "labels"))
    cli.state.write_bytes(b"sensitive-corrupted-local-metadata")
    error = cli.invoke("list", "--file", cli.state, "--limit", "2", "--json", "labels",
                       "--cursor", first["next_cursor"], code=2)
    assert error["error"] == "resource_labels_cursor_query_changed"
    assert "sensitive-corrupted-local-metadata" not in json.dumps(error)

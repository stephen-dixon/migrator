"""Protocol-free tests of the language-server engine."""
import json

from schemamig import jsonpos
from schemamig.lsp import Engine, target_of

from .test_journal import KIND, P, e_rename_probe, edit, proj  # noqa: F401  (fixture)


def _text(root):
    return (root / f"schemas/{KIND}.schema.json").read_text()


def _path(root):
    return root / f"schemas/{KIND}.schema.json"


def test_jsonpos_index_and_lookup():
    text = '{\n  "a": {\n    "b": [1, {"c": "x"}]\n  }\n}\n'
    idx = jsonpos.index(text)
    assert idx["#/a/b/1/c"].key == ((2, 14), (2, 17))
    assert jsonpos.at_position(idx, 2, 16) == "#/a/b/1/c"
    assert jsonpos.locate(idx, "#/a/missing/deeper").ptr == "#/a"


def test_target_of():
    assert target_of("#/$defs/dimension/properties/probe") == ("def:dimension", "probe")
    assert target_of("#/properties/metadata/properties/x/enum/0") == ("#/properties/metadata", "x")
    assert target_of("#/patternProperties") == ("$", None)


def test_unrecorded_edit_is_a_warning_at_the_edit(proj):
    edit(proj, e_rename_probe)
    text = _text(proj)
    diags = Engine().diagnostics(_path(proj), text)
    warn = [d for d in diags if d.get("code") == "unrecorded"]
    assert warn and all(d["severity"] == 2 for d in warn)
    lines = [text.splitlines()[d["range"]["start"]["line"]] for d in warn]
    assert any('"probe"' in line for line in lines)  # added key: at the key; removed key: at its parent


def test_record_quickfix_offers_only_verified_classes_and_works(proj):
    edit(proj, e_rename_probe)
    text = _text(proj)
    line = next(i for i, l in enumerate(text.splitlines()) if '"probe"' in l)
    eng = Engine()
    acts = eng.code_actions(_path(proj), text, line, 10)
    record = [a for a in acts if a["command"] == "schemamig.record"]
    titles = [a["title"] for a in record]
    assert any("record as rename-field: rename field 'dim_probe' -> 'probe'" in t for t in titles), titles
    assert not any("record as remove-field" in t for t in titles)  # does not explain the edit
    assert titles[-1].startswith("schemamig: record as unclassified")
    args = next(a for a in record if "rename-field" in a["title"])["arguments"][0]
    res = eng.execute(proj, "schemamig.record", {**args, "reason": "naming", "path": str(_path(proj))})
    assert res["ok"] and res["record"]["ops"][0]["to"] == "probe"
    assert [d for d in eng.diagnostics(_path(proj), _text(proj)) if d.get("code") == "unrecorded"] == []


def test_open_change_shows_info_and_commit_action(proj):
    eng = Engine()
    path = str(_path(proj))
    res = eng.execute(proj, "schemamig.begin", {"class": "rename-field", "target": "def:dimension", "path": path})
    assert res["ok"] and res["status"]["problem"] == "waiting for your edit"
    edit(proj, e_rename_probe)
    diags = eng.diagnostics(_path(proj), _text(proj))
    assert [d["code"] for d in diags] == ["open-change"] and "ready to commit" in diags[0]["message"]
    acts = eng.code_actions(_path(proj), _text(proj), 0, 0)
    assert acts[0]["command"] == "schemamig.commit" and acts[0]["arguments"][0]["prompt_reason"] is True
    assert eng.execute(proj, "schemamig.commit", {"reason": "naming"})["ok"]


def test_unsaved_buffer_is_checked_not_the_file(proj):
    s = json.loads(_text(proj))
    e_rename_probe(s)
    buffer = json.dumps(s, indent=2)  # edited in the editor, not saved
    assert any(d.get("code") == "unrecorded" for d in Engine().diagnostics(_path(proj), buffer))
    assert [d for d in Engine().diagnostics(_path(proj), _text(proj)) if d.get("code") == "unrecorded"] == []


def test_invalid_json_buffer_reports_parse_error(proj):
    diags = Engine().diagnostics(_path(proj), '{"a": ')
    assert diags[0]["severity"] == 1 and "invalid JSON" in diags[0]["message"]


def test_non_schema_files_are_ignored(proj):
    assert Engine().diagnostics(proj / "fixtures/mappings/1.0.0/equilibrium.json", "{}") == []

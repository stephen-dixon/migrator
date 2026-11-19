"""Journal workflow: granular change scopes over a hand-edited working schema."""
import json
import shutil

import pytest
import yaml

from schemamig import journal as J
from schemamig.cli import main
from schemamig.migrate import migrate
from schemamig.project import Project, load_json
from schemamig.schemadiff import equivalent

from .conftest import EXAMPLE

KIND = "tokamap-mappings"


@pytest.fixture
def proj(tmp_path):
    """Example project rewound to mappings 1.1.0, with a hand-edited working schema + journal."""
    root = tmp_path / "tokamap"
    shutil.copytree(EXAMPLE, root)
    for f in ("0003-1.1.0-to-2.0.0.yaml", "0004-2.0.0-to-2.1.0.yaml"):
        (root / "migrations/mappings" / f).unlink()
    shutil.rmtree(root / "fixtures/mappings/2.1.0")
    for v in ("2.0.0", "2.1.0"):
        (root / f"generated/{KIND}-{v}.schema.json").unlink()
    (root / "schemas").mkdir(exist_ok=True)
    shutil.copy(root / f"generated/{KIND}-1.1.0.schema.json", root / f"schemas/{KIND}.schema.json")
    cfg = yaml.safe_load((root / "schemamig.yaml").read_text())
    cfg["kinds"][KIND].update(schema=f"schemas/{KIND}.schema.json", journal=f"changes/{KIND}.jsonl")
    (root / "schemamig.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return root


def P(root):
    return Project(root / "schemamig.yaml")


def edit(root, fn):
    """Simulate the developer editing the working schema in their editor."""
    path = root / f"schemas/{KIND}.schema.json"
    s = json.loads(path.read_text())
    r = fn(s)
    s = r if isinstance(r, dict) and "$defs" in r else s  # fn mutates, or returns a whole new schema
    path.write_text(json.dumps(s, indent=2) + "\n")


def renamed(d, old, new, value=None):
    return {(new if k == old else k): (value if (k == old and value is not None) else v) for k, v in d.items()}


def change(root, cls, target, editfn, **opts):
    p = P(root)
    J.begin(p, p.kind(KIND), cls, target, opts, reason=opts.pop("reason", f"{cls} {target}"))
    edit(root, editfn)
    return J.commit(p)


# --- the six edits of mappings 1.1.0 -> 2.0.0, as a developer would make them ---------------

def e_rename_probe(s):
    d = s["$defs"]["dimension"]
    d["properties"] = renamed(d["properties"], "dim_probe", "probe")
    d["required"] = ["probe" if r == "dim_probe" else r for r in d["required"]]


def e_expr(s):
    s["$defs"]["expression"]["properties"]["map_type"]["const"] = "EXPRESSION"


def e_nest(s):
    props = s["$defs"]["data_source"]["properties"]
    container = {"description": "Linear transform applied to the data (value * scale + offset)",
                 "type": "object", "properties": {"offset": props["offset"], "scale": props["scale"]},
                 "additionalProperties": False}
    s["$defs"]["data_source"]["properties"] = {
        k: v for k, v in renamed(props, "offset", "transform", container).items() if k != "scale"}


def e_wrap(s):
    hoist = {k: s[k] for k in ("$schema", "$id", "description")}
    inner = {k: v for k, v in s.items() if k not in ("$schema", "$id", "description", "$defs")}
    return {**hoist, "type": "object", "properties": {"mappings": inner}, "required": ["mappings"],
            "additionalProperties": False, "$defs": s["$defs"]}


def e_kind(s):
    s["properties"] = {"kind": {"const": "tokamap.mappings"}, **s["properties"]}
    s["required"].append("kind")


def e_format(s):
    s["properties"]["format_version"] = {"type": "string", "pattern": "^\\d+\\.\\d+\\.\\d+$"}
    s["required"].append("format_version")


def author_2_0_0(root):
    change(root, "rename-field", "def:dimension", e_rename_probe, reason="consistent naming")
    change(root, "rename-value", "def:expression", e_expr, name="map_type")
    change(root, "nest-fields", "def:data_source", e_nest)
    change(root, "wrap", "$", e_wrap, reason="envelope the bare mapping object")
    change(root, "add-field", "$", e_kind, default="tokamap.mappings")
    change(root, "add-field", "$", e_format, managed_version=True)


def test_journal_reauthoring_matches_the_yaml_changeset(proj):
    author_2_0_0(proj)
    p = P(proj)
    k = p.kind(KIND)
    assert len(J.effective(J.read_records(k))) == 6
    assert J.check(p, k) == ([], [])
    rec = J.release(p, k, "2.0.0", compatibility="breaking")

    released = yaml.safe_load(rec.read_text())
    assert released["version_path"] == "/format_version"
    assert [c["class"] for c in released["changes"]] == [
        "rename-field", "rename-value", "nest-fields", "wrap", "add-field", "add-field"]
    assert (proj / f"changes/{KIND}.jsonl").read_text() == ""

    k = P(proj).kind(KIND)
    yaml_kind = Project(EXAMPLE / "schemamig.yaml").kind(KIND)
    assert equivalent(k.schemas()["2.0.0"], yaml_kind.schemas()["2.0.0"])
    for f in ("equilibrium.json", "magnetics-40.json"):
        doc = load_json(EXAMPLE / "fixtures/mappings/1.0.0" / f)
        mine, _ = migrate(k, doc, to="2.0.0")
        theirs, _ = migrate(yaml_kind, doc, to="2.0.0")
        assert mine == theirs and list(mine) == list(theirs), f


def test_release_snapshot_keeps_hand_formatting(proj):
    path = proj / f"schemas/{KIND}.schema.json"
    text = path.read_text().replace('"type": "string"\n        }', '"type": "string" }')  # some hand style
    path.write_text(text)
    change(proj, "rename-value", "def:expression", e_expr, name="map_type")
    before = path.read_text()
    p = P(proj)
    J.release(p, p.kind(KIND), "1.2.0", compatibility="breaking")
    snap = (proj / f"releases/{KIND}-1.2.0.schema.json").read_text()
    assert snap == before.replace("urn:tokamap-mappings:1.1.0", "urn:tokamap-mappings:1.2.0")
    assert path.read_text() == snap  # working file: only $id changed


def test_tampered_snapshot_is_detected(proj):
    change(proj, "rename-value", "def:expression", e_expr, name="map_type")
    p = P(proj)
    J.release(p, p.kind(KIND), "1.2.0")
    snap = proj / f"releases/{KIND}-1.2.0.schema.json"
    snap.write_text(snap.read_text().replace('"const": "VALUE"', '"const": "VAL"'))
    with pytest.raises(Exception, match="do not replay to the schema snapshot"):
        P(proj).kind(KIND).schemas()


def test_annotation_edits_need_no_record(proj):
    edit(proj, lambda s: s["$defs"]["dimension"].__setitem__("description", "A dimension probe"))
    p = P(proj)
    assert J.check(p, p.kind(KIND))[0] == []


def test_unrecorded_edit_fails_check_and_blocks_unrelated_commit(proj):
    edit(proj, lambda s: s["$defs"]["value"]["properties"].pop("comment"))
    p = P(proj)
    errs, _ = J.check(p, p.kind(KIND))
    assert any("not explained by the journal" in e and "/$defs/value/properties/comment" in e for e in errs)
    with pytest.raises(J.JournalError, match="does not explain"):
        change(proj, "rename-value", "def:expression", e_expr, name="map_type")


def test_begin_after_editing_works_because_preimage_is_the_replay(proj):
    edit(proj, e_rename_probe)  # edited first...
    p = P(proj)
    J.begin(p, p.kind(KIND), "rename-field", "def:dimension", {})  # ...declared afterwards
    rec = J.commit(p, reason="late declaration")
    assert rec["ops"] == [{"op": "rename-field", "at": "def:dimension", "from": "dim_probe", "to": "probe"}]


def test_wrong_class_for_the_edit_is_refused(proj):
    p = P(proj)
    J.begin(p, p.kind(KIND), "remove-field", "def:dimension", {})
    edit(proj, e_rename_probe)
    with pytest.raises(J.JournalError, match="does not explain"):
        J.commit(p)
    assert J.status(p)["ready"] is False


def test_required_add_without_default_refused(proj):
    def add_req(s):
        s["$defs"]["value"]["properties"]["unit"] = {"type": "string"}
        s["$defs"]["value"]["required"].append("unit")
    with pytest.raises(J.JournalError, match="no --default"):
        change(proj, "add-field", "def:value", add_req)


def test_reopen_amends_last_change(proj):
    rec = change(proj, "rename-field", "def:dimension", e_rename_probe)
    p = P(proj)
    J.reopen(p, p.kind(KIND), rec["id"])
    edit(proj, lambda s: s["$defs"]["dimension"].__setitem__(
        "properties", renamed(s["$defs"]["dimension"]["properties"], "probe", "probe_key")) or
        s["$defs"]["dimension"].__setitem__("required", ["map_type", "probe_key"]))
    amend = J.commit(p)
    assert amend["type"] == "amend" and amend["amends"] == rec["id"]
    eff = J.effective(J.read_records(p.kind(KIND)))
    assert len(eff) == 1 and eff[0]["ops"][0]["to"] == "probe_key"
    assert J.check(p, p.kind(KIND))[0] == []


def test_reopen_refused_when_later_change_overlaps(proj):
    first = change(proj, "rename-field", "def:dimension", e_rename_probe)
    # sibling locations don't overlap: an unrelated field added to the same object
    change(proj, "add-field", "def:dimension", lambda s: s["$defs"]["dimension"]["properties"].__setitem__(
        "units", {"type": "string"}))
    p = P(proj)
    J.reopen(p, p.kind(KIND), first["id"])
    J.abort(p)
    # ...but renaming the renamed field again does
    change(proj, "rename-field", "def:dimension", lambda s: s["$defs"]["dimension"].update(
        properties=renamed(s["$defs"]["dimension"]["properties"], "probe", "probe_key"),
        required=["map_type", "probe_key"]))
    with pytest.raises(J.JournalError, match="touch the same schema locations"):
        J.reopen(p, p.kind(KIND), first["id"])
    # a non-overlapping earlier change can still be reopened
    other = change(proj, "rename-value", "def:expression", e_expr, name="map_type")
    change(proj, "add-field", "def:custom", lambda s: s["$defs"]["custom"]["properties"].__setitem__(
        "version", {"type": "string"}))
    J.reopen(p, p.kind(KIND), other["id"])
    J.abort(p)


def test_drop_requires_reverting_the_edit(proj):
    rec = change(proj, "rename-value", "def:expression", e_expr, name="map_type")
    p = P(proj)
    J.drop(p.kind(KIND), rec["id"])
    assert any("not explained" in e for e in J.check(p, p.kind(KIND))[0])
    edit(proj, lambda s: s["$defs"]["expression"]["properties"]["map_type"].__setitem__("const", "EXPR"))
    assert J.check(p, p.kind(KIND))[0] == []


def _union_merge(root, ours, theirs):
    """What `merge=union` produces: base lines, then both sides' appended lines."""
    (root / f"changes/{KIND}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ours + theirs))


def test_concurrent_overlapping_changes_detected_after_union_merge(proj):
    a = change(proj, "rename-field", "def:dimension", e_rename_probe)
    journal = proj / f"changes/{KIND}.jsonl"
    # branch B, written on the same base (after=None), also touches $defs/dimension
    b = {**a, "id": "cbbbbbbbb", "class": "make-required", "target": "def:dimension",
         "ops": [{"op": "make-required", "at": "def:dimension", "name": "comment", "default": ""}]}
    _union_merge(proj, [a], [b])  # both edit $defs/dimension/required
    edit(proj, lambda s: s["$defs"]["dimension"]["required"].append("comment"))
    p = P(proj)
    errs, _ = J.check(p, p.kind(KIND))
    assert any("written concurrently" in e for e in errs), errs
    # resolution: declare the order by re-pointing the later record
    recs = [json.loads(line) for line in journal.read_text().splitlines()]
    recs[1]["after"] = recs[0]["id"]
    journal.write_text("".join(json.dumps(r) + "\n" for r in recs))
    assert J.check(p, p.kind(KIND))[0] == []


def test_concurrent_disjoint_changes_are_fine(proj):
    a = change(proj, "rename-field", "def:dimension", e_rename_probe)
    b = {**a, "id": "cbbbbbbbb", "class": "rename-value", "target": "def:expression",
         "ops": [{"op": "rename-value", "at": "def:expression", "name": "map_type", "values": {"EXPR": "EXPRESSION"}}]}
    _union_merge(proj, [a], [b])
    edit(proj, e_expr)
    p = P(proj)
    assert J.check(p, p.kind(KIND))[0] == []


def test_check_runs_pending_changes_over_head_fixtures(proj):
    # head fixtures: put the 1.0.0 fixture migrated to 1.1.0 in place
    k = P(proj).kind(KIND)
    out, _ = migrate(k, load_json(proj / "fixtures/mappings/1.0.0/equilibrium.json"), to="1.1.0")
    (proj / "fixtures/mappings/1.1.0").mkdir()
    (proj / "fixtures/mappings/1.1.0/equilibrium.json").write_text(json.dumps(out, indent=2) + "\n")
    # tighten scale to number: the fixture's "0.5"-style string offset/scale breaks
    change(proj, "set-field-schema", "def:data_source",
           lambda s: s["$defs"]["data_source"]["properties"].__setitem__("offset", {"type": "number"}))
    p = P(proj)
    errs, _ = J.check(p, p.kind(KIND))
    assert any("fixture equilibrium.json" in e for e in errs), errs


def test_unclassified_change_becomes_escape_hatch(proj):
    def anything(s):
        s["$defs"]["value"]["properties"]["value"]["type"] = ["number", "string", "array", "boolean"]
    with pytest.raises(J.JournalError, match="need --reason"):
        change(proj, "unclassified", "-", anything, reason="")
    J.abort(P(proj))
    rec = change(proj, "unclassified", "-", anything, reason="allow boolean VALUE mappings")
    assert rec["ops"][0]["op"] == "patch-schema"
    p = P(proj)
    J.release(p, p.kind(KIND), "1.2.0", compatibility="backward-compatible")
    e = P(proj).kind(KIND).chain()[-1]
    assert e.assessment()["implementation"] == "declarative + escape hatch"


def test_add_variant_with_new_def(proj):
    def add_interp(s):
        s["$defs"]["interp"] = {"properties": {"map_type": {"const": "INTERP"}, "source": {"type": "string"}},
                                "required": ["map_type", "source"], "additionalProperties": False}
        s["$defs"]["map_type"]["oneOf"].append({"$ref": "#/$defs/interp"})
    rec = change(proj, "add-variant", "def:map_type", add_interp)
    assert [o["op"] for o in rec["ops"]] == ["add-def", "add-variant"]


@pytest.fixture
def config_proj(tmp_path):
    root = tmp_path / "tokamap"
    shutil.copytree(EXAMPLE, root)
    return root


def _set_enum(root, values):
    path = root / "schemas/tokamap-config.schema.json"
    s = json.loads(path.read_text())
    s["$defs"]["rule"]["properties"]["selector"]["enum"] = values
    path.write_text(json.dumps(s, indent=2))


def test_rename_value_single_change_is_inferred_even_if_reordered(config_proj):
    p = Project(config_proj / "schemamig.yaml")
    J.begin(p, p.kind("tokamap-config"), "rename-value", "def:rule", {"name": "selector"})
    _set_enum(config_proj, ["exact", "MAX_BELOW", "min_above", "closest"])
    assert J.commit(p)["ops"][0]["values"] == {"max_below": "MAX_BELOW"}


def test_rename_value_several_changes_need_explicit_map(config_proj):
    p = Project(config_proj / "schemamig.yaml")
    k = p.kind("tokamap-config")
    J.begin(p, k, "rename-value", "def:rule", {"name": "selector"})
    _set_enum(config_proj, ["MAX_BELOW", "min_above", "exact", "CLOSEST"])
    with pytest.raises(J.JournalError, match="--map OLD=NEW"):
        J.commit(p)
    J.abort(p)
    J.begin(p, k, "rename-value", "def:rule",
            {"name": "selector", "values": {"max_below": "MAX_BELOW", "closest": "CLOSEST"}})
    assert J.commit(p)["ops"][0]["values"] == {"max_below": "MAX_BELOW", "closest": "CLOSEST"}


def test_value_swap_is_invisible_to_the_schema_only_the_explicit_record_carries_it(config_proj):
    p = Project(config_proj / "schemamig.yaml")
    J.begin(p, p.kind("tokamap-config"), "rename-value", "def:rule",
            {"name": "selector", "values": {"max_below": "CLOSEST", "closest": "MAX_BELOW"}})  # swapped by mistake
    _set_enum(config_proj, ["MAX_BELOW", "min_above", "exact", "CLOSEST"])
    # the swap yields the same *set* of values, so the schema alone cannot tell -- but the
    # recorded intent is explicit and is what migrates documents; show it for audit
    rec = J.commit(p)
    assert rec["options"]["values"] == {"max_below": "CLOSEST", "closest": "MAX_BELOW"}


def test_explicit_rename_field_names(proj):
    p = P(proj)
    J.begin(p, p.kind(KIND), "rename-field", "def:dimension", {"from": "dim_probe", "to": "probe"})
    edit(proj, e_rename_probe)
    assert J.commit(p)["ops"][0] == {"op": "rename-field", "at": "def:dimension", "from": "dim_probe", "to": "probe"}
    J.begin(p, p.kind(KIND), "rename-field", "def:dimension", {"from": "comment", "to": "note"})
    edit(proj, lambda s: s["$defs"]["dimension"].update(
        properties=renamed(s["$defs"]["dimension"]["properties"], "comment", "remark")))
    with pytest.raises(J.JournalError, match="does not explain"):
        J.commit(p)


def test_cli_session_flow(proj, capsys):
    cfg = ["--project", str(proj / "schemamig.yaml")]
    assert main(cfg + ["change", "begin", "rename-field", "def:dimension", "--kind", KIND,
                       "--reason", "naming"]) == 0
    assert main(cfg + ["change", "status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert st["open"] and st["ready"] is False
    edit(proj, e_rename_probe)
    assert main(cfg + ["change", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    assert main(cfg + ["change", "commit"]) == 0
    assert main(cfg + ["check", "--kind", KIND]) == 0
    edit(proj, e_expr)
    assert main(cfg + ["check", "--kind", KIND]) == 1
    assert main(cfg + ["change", "record", "rename-value", "def:expression", "--kind", KIND,
                       "--name", "map_type", "--map", "EXPR=EXPRESSION"]) == 0
    assert main(cfg + ["check", "--kind", KIND]) == 0
    assert main(cfg + ["release", "2.0.0", "--kind", KIND, "--compat", "breaking"]) == 0
    assert main(cfg + ["check", "--kind", KIND]) == 0

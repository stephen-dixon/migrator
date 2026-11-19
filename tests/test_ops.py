"""Brief section 7 minimum cases plus the rest of the operation vocabulary."""
import copy
import json

import pytest

from schemamig.migrate import migrate
from schemamig.ops import DslError, MigrationError
from schemamig.schema import SchemaError, check_supported, errors

from .conftest import ROOT

DOC = {"v": "1.0.0",
       "ip": {"kind": "SOURCE", "source": "uda", "scale": "2", "note": "x"},
       "r_major": {"kind": "VALUE", "value": 850, "unit": "MM"}}


def edge(from_, to, *ops, **kw):
    return {"from": from_, "to": to, "operations": list(ops), **kw}


# --- add-field -----------------------------------------------------------------

def test_add_optional_field_leaves_old_instances_untouched(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:source", "name": "units", "type": "string"})])
    s = k.schemas()["1.1.0"]
    assert s["$defs"]["source"]["properties"]["units"] == {"type": "string"}
    assert "units" not in s["$defs"]["source"]["required"]
    out, rep = migrate(k, copy.deepcopy(DOC))
    assert out == {**DOC, "v": "1.1.0"}
    assert rep.edges[0].changes == []


def test_add_required_field_with_default_inserts_it(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:value", "name": "unit2", "type": "string",
                      "required": True, "default": "SI"})])
    out, _ = migrate(k, copy.deepcopy(DOC))
    assert out["r_major"]["unit2"] == "SI"
    assert "unit2" not in out["ip"]  # only the governed variant is touched


def test_add_required_field_without_default_is_refused(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:value", "name": "x", "type": "string", "required": True})])
    with pytest.raises(DslError, match="without a default cannot be migrated"):
        k.schemas()


def test_add_field_default_must_match_schema(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:value", "name": "x", "type": "integer",
                      "required": True, "default": "nope"})])
    with pytest.raises(DslError, match="default"):
        k.schemas()


# --- rename-field -------------------------------------------------------------------

def test_rename_nested_field_updates_schema_instance_and_required(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "source", "to": "data_source"})])
    s = k.schemas()["1.1.0"]["$defs"]["source"]
    assert list(s["properties"]) == ["kind", "data_source", "scale", "note"]  # position kept
    assert s["required"] == ["kind", "data_source"]
    out, rep = migrate(k, copy.deepcopy(DOC))
    assert list(out["ip"]) == ["kind", "data_source", "scale", "note"]  # key order kept
    assert out["ip"]["data_source"] == "uda"
    assert [str(c) for c in rep.edges[0].changes] == ["rename-field $[\"ip\"]: 'source' -> 'data_source'"]


def test_rename_conflict_fails_without_data_loss(make_project, mappings_schema):
    mappings_schema["$defs"]["source"]["additionalProperties"] = True
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "source", "to": "data_source"})])
    doc = copy.deepcopy(DOC)
    doc["ip"]["data_source"] = "other"
    before = copy.deepcopy(doc)
    with pytest.raises(MigrationError, match="both 'source' and 'data_source' present"):
        migrate(k, doc)
    assert doc == before


def test_rename_missing_optional_source_is_noop(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "note", "to": "comment"})])
    doc = copy.deepcopy(DOC)
    del doc["ip"]["note"]
    out, rep = migrate(k, doc)
    assert out["ip"] == doc["ip"] and rep.edges[0].changes == []


def test_rename_onto_existing_property_is_refused(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "note", "to": "scale"})])
    with pytest.raises(DslError, match="already exists"):
        k.schemas()


# --- remove-field --------------------------------------------------------------------

def test_removal_without_approval_fails_when_data_present(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "remove-field", "at": "def:source", "name": "note"})])
    with pytest.raises(MigrationError, match="set 'lossy: true'"):
        migrate(k, copy.deepcopy(DOC))
    doc = copy.deepcopy(DOC)
    del doc["ip"]["note"]
    out, _ = migrate(k, doc)  # nothing to lose -> fine
    assert "note" not in k.schemas()["1.1.0"]["$defs"]["source"]["properties"]


def test_lossy_removal_with_opt_in_reports_lost_values(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "remove-field", "at": "def:source", "name": "note", "lossy": True})])
    out, rep = migrate(k, copy.deepcopy(DOC))
    assert "note" not in out["ip"]
    lost = rep.lossy()
    assert len(lost) == 1 and lost[0].path == '$["ip"]["note"]' and "'x'" in lost[0].detail
    assert "LOSSY" in k.chain()[0].assessment()["instance_migration"]


# --- validation & versions -------------------------------------------------------------------

def test_invalid_old_document_is_rejected_before_migration(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:source", "name": "u", "type": "string"})])
    doc = copy.deepcopy(DOC)
    doc["ip"]["bogus"] = 1
    with pytest.raises(MigrationError, match="not valid under its declared version 1.0.0"):
        migrate(k, doc)


def test_invalid_migrated_document_is_rejected_and_names_the_op(make_project, mappings_schema):
    # tightening scale to number without a converter: "2" no longer validates
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "set-field-schema", "at": "def:source", "name": "scale", "schema": {"type": "number"}})])
    with pytest.raises(MigrationError, match=r"op #1 \(replace schema of 'scale'"):
        migrate(k, copy.deepcopy(DOC))


def test_set_field_schema_with_converter(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "set-field-schema", "at": "def:source", "name": "scale",
                      "schema": {"type": "number"}, "convert": "to-number"})])
    out, _ = migrate(k, copy.deepcopy(DOC))
    assert out["ip"]["scale"] == 2
    doc = copy.deepcopy(DOC)
    doc["ip"]["scale"] = "{{ factor }}"
    with pytest.raises(MigrationError, match="cannot convert"):
        migrate(k, doc)


def test_chaining_two_edges(make_project, mappings_schema):
    k = make_project(mappings_schema, [
        edge("1.0.0", "1.1.0", {"op": "rename-field", "at": "def:source", "from": "source", "to": "ds"}),
        edge("1.1.0", "2.0.0", {"op": "rename-field", "at": "def:source", "from": "ds", "to": "data_source"},
             {"op": "rename-value", "at": "def:value", "name": "unit", "values": {"MM": "mm", "M": "m"}}),
    ])
    out, rep = migrate(k, copy.deepcopy(DOC))
    assert out["v"] == "2.0.0" and out["ip"]["data_source"] == "uda" and out["r_major"]["unit"] == "mm"
    assert [e.name for e in rep.edges] == ["t 1.0.0 -> 1.1.0", "t 1.1.0 -> 2.0.0"]
    # partial target
    mid, _ = migrate(k, copy.deepcopy(DOC), to="1.1.0")
    assert mid["v"] == "1.1.0" and mid["ip"]["ds"] == "uda"
    # and continuing from the middle reaches the same result
    assert migrate(k, mid)[0] == out


def test_already_at_target_is_explicit_noop_never_reapplied(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "source", "to": "ds"})])
    out, _ = migrate(k, copy.deepcopy(DOC))
    again, rep = migrate(k, out)
    assert rep.noop and again == out


def test_unknown_and_missing_versions(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:source", "name": "u", "type": "string"})])
    with pytest.raises(MigrationError, match="unknown document version"):
        migrate(k, {**DOC, "v": "9.9.9"})
    with pytest.raises(MigrationError, match="pass --from-version"):
        migrate(k, {k2: v for k2, v in DOC.items() if k2 != "v"})
    with pytest.raises(MigrationError, match="contradicts"):
        migrate(k, copy.deepcopy(DOC), from_version="1.1.0")
    with pytest.raises(MigrationError, match="downgrades"):
        migrate(k, {**DOC, "v": "1.1.0"}, to="1.0.0")


def test_chain_must_be_linear_and_connected(make_project, mappings_schema):
    op = {"op": "add-field", "at": "def:source", "name": "u", "type": "string"}
    with pytest.raises(DslError, match="two edges leave 1.0.0"):
        make_project(mappings_schema, [edge("1.0.0", "1.1.0", op), edge("1.0.0", "1.2.0", op)]).chain()


def test_wrong_document_kind_rejected(make_project, mappings_schema):
    k = make_project(mappings_schema, [{"document_kind": "other", **edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "def:source", "name": "u", "type": "string"})}])
    with pytest.raises(DslError, match="document_kind"):
        k.chain()


# --- compatibility claims ---------------------------------------------------------------------

def test_backward_compatible_claim_contradicted_by_rewrite(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "def:source", "from": "source", "to": "ds"},
                     compatibility="backward-compatible")])
    with pytest.raises(DslError, match="contradicting the backward-compatible claim"):
        k.schemas()


def test_backward_compatible_claim_falsified_by_fixture(make_project, mappings_schema):
    from schemamig.verify import verify
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "set-field-schema", "at": "def:source", "name": "scale", "schema": {"type": "number"}},
                     compatibility="backward-compatible")],
                     files={"fixtures/1.0.0/a.json": DOC})
    problems = verify(k)
    assert any("claimed backward-compatible" in p for p in problems), problems


# --- other ops -------------------------------------------------------------------------------

def test_rename_value_on_discriminator_and_enum_collision(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-value", "at": "def:value", "name": "kind", "values": {"VALUE": "CONST"}})])
    out, _ = migrate(k, copy.deepcopy(DOC))
    assert out["r_major"]["kind"] == "CONST" and out["ip"]["kind"] == "SOURCE"
    k2 = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                      {"op": "rename-value", "at": "def:value", "name": "unit", "values": {"MM": "M"}})])
    with pytest.raises(DslError, match="already in enum"):
        k2.schemas()


def test_nest_fields(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "nest-fields", "at": "def:source", "fields": ["source", "scale"], "into": "src"})])
    s = k.schemas()["1.1.0"]["$defs"]["source"]
    assert s["required"] == ["kind", "src"] and s["properties"]["src"]["required"] == ["source"]
    out, _ = migrate(k, copy.deepcopy(DOC))
    assert out["ip"] == {"kind": "SOURCE", "src": {"source": "uda", "scale": "2"}, "note": "x"}


def test_wrap_root_and_move_version_field(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge(
        "1.0.0", "2.0.0",
        {"op": "remove-field", "at": "$", "name": "v"},  # the old version slot is engine-stripped
        {"op": "wrap", "at": "$", "into": "mappings"},
        {"op": "add-field", "at": "$", "name": "format", "type": "string", "required": True,
         "managed": "version"},
        version_path="/format")])
    out, _ = migrate(k, copy.deepcopy(DOC))
    assert out == {"format": "2.0.0", "mappings": {k2: v for k2, v in DOC.items() if k2 != "v"}}
    s = k.schemas()["2.0.0"]
    assert "$defs" in s and "$defs" not in s["properties"]["mappings"]


def test_add_variant_that_overlaps_breaks_oneof_and_is_caught(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-variant", "at": "def:entry", "schema": {"type": "object"}})])
    with pytest.raises(MigrationError, match="not valid afterwards"):
        migrate(k, copy.deepcopy(DOC))


def test_make_required_and_optional(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "make-required", "at": "def:value", "name": "unit", "default": "M"},
                     {"op": "make-optional", "at": "def:source", "name": "source"})])
    doc = copy.deepcopy(DOC)
    del doc["r_major"]["unit"]
    out, _ = migrate(k, doc)
    assert out["r_major"]["unit"] == "M"
    assert k.schemas()["1.1.0"]["$defs"]["source"]["required"] == ["kind"]


def test_unknown_op_and_unknown_keys(make_project, mappings_schema):
    with pytest.raises(DslError, match="unknown operation 'move-everything'"):
        make_project(mappings_schema, [edge("1.0.0", "1.1.0", {"op": "move-everything"})]).chain()
    with pytest.raises(DslError, match=r"unknown key\(s\) \['form'\]"):
        make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "$", "form": "a", "from": "a", "to": "b"})]).chain()


# --- unsupported constructs & addressing ---------------------------------------------------------

def test_unsupported_constructs_rejected_with_explanation():
    with pytest.raises(SchemaError, match="'allOf' is not supported.*ownership"):
        check_supported({"allOf": [{"type": "object"}]})
    with pytest.raises(SchemaError, match="only local"):
        check_supported({"properties": {"a": {"$ref": "https://example.com/s.json"}}})
    with pytest.raises(SchemaError, match="'if' is not supported"):
        check_supported({"properties": {"a": {"if": {}, "then": {}}}})


def test_real_libtokamap_globals_schema_bug_is_detected():
    real = json.loads((ROOT / "tests/data/libtokamap-globals.schema.json").read_text())
    with pytest.raises(SchemaError, match="unknown keyword 'args'"):
        check_supported(real)


def test_real_libtokamap_mappings_schema_bug_is_linted():
    base = json.loads((ROOT / "examples/tokamap/baselines/tokamap-mappings-1.0.0.json").read_text())
    warnings = check_supported(base)
    assert any("named like the keyword 'additionalProperties'" in w for w in warnings)


def test_instance_path_cannot_cross_union(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "add-field", "at": "$.*", "name": "u", "type": "string"})])
    with pytest.raises(DslError, match="is a union; target one variant"):
        k.schemas()


def test_patch_schema_cannot_introduce_unsupported_constructs(make_project, mappings_schema):
    k = make_project(mappings_schema, [edge("1.0.0", "1.1.0",
                     {"op": "patch-schema", "reason": "x",
                      "patch": [{"op": "add", "path": "/$defs/value/allOf", "value": []}]})])
    with pytest.raises(DslError, match="allOf"):
        k.schemas()


def test_shared_def_change_is_flagged(make_project):
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/p"}, "b": {"$ref": "#/$defs/p"},
                                               "v": {"type": "string"}},
              "$defs": {"p": {"type": "object", "properties": {"x": {"type": "string"}}}}}
    k = make_project(schema, [edge("1.0.0", "1.1.0",
                     {"op": "rename-field", "at": "$.a", "from": "x", "to": "y"})])
    k.schemas()
    assert any("shared by 2 $ref users" in w for w in k.warnings)
    out, _ = migrate(k, {"v": "1.0.0", "a": {"x": "1"}, "b": {"x": "2"}})
    assert out == {"v": "1.1.0", "a": {"y": "1"}, "b": {"y": "2"}}  # both users migrate


def test_errors_helper_reports_paths(mappings_schema):
    errs = errors({"$id": "urn:t:1.0.0", **mappings_schema}, {"v": "1", "x": {"kind": "VALUE"}})
    assert errs and errs[0].path == '$["x"]'

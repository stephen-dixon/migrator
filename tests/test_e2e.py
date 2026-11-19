"""End-to-end: the example project modelled on libtokamap's real schemas and fixtures."""
import copy
import hashlib
import json
import shutil

import pytest

from schemamig.cli import main
from schemamig.impact import impact
from schemamig.migrate import migrate
from schemamig.ops import DslError, MigrationError
from schemamig.project import Project, generate
from schemamig.schema import SchemaError
from schemamig.verify import verify

from .conftest import EXAMPLE


@pytest.fixture
def project():
    return Project(EXAMPLE / "schemamig.yaml")


@pytest.fixture
def proj_copy(tmp_path):
    dst = tmp_path / "tokamap"
    shutil.copytree(EXAMPLE, dst)
    return dst


def load(rel):
    return json.loads((EXAMPLE / rel).read_text())


def test_fixtures_goldens_and_claims_verify(project):
    for k in project.kinds.values():
        assert verify(k) == [], k.name


def test_committed_generated_schemas_are_up_to_date(project):
    for k in project.kinds.values():
        assert all(status == "ok" for _, status in generate(k, write=False)), k.name


def test_real_magnetics_mapping_migrates_end_to_end(project):
    k = project.kind("tokamap-mappings")
    doc = load("fixtures/mappings/1.0.0/magnetics-40.json")
    out, rep = migrate(k, doc)
    assert list(out) == ["format_version", "kind", "mappings"]
    assert out["format_version"] == "2.1.0" and "version" not in out["mappings"]
    assert out["mappings"]["coil"] == {"map_type": "DIMENSION", "probe": "_coils"}
    assert out["mappings"]["coil[#]/area"]["map_type"] == "EXPRESSION"
    # every original mapping key survives
    assert set(out["mappings"]) == set(doc) - {"version"}


def test_slice_adapter_keeps_templated_slices(project):
    k = project.kind("tokamap-mappings")
    out, _ = migrate(k, load("fixtures/mappings/1.0.0/equilibrium.json"))
    assert out["mappings"]["time"]["slice"] == {"start": 0, "stop": 100}
    assert out["mappings"]["psi_axis"]["slice"] == "[{{ indices.axis }}]"


def test_slice_adapter_refuses_unrecognised_syntax(project):
    k = project.kind("tokamap-mappings")
    doc = load("fixtures/mappings/1.0.0/equilibrium.json")
    doc["time"]["slice"] = "first 100"
    with pytest.raises(MigrationError, match="unrecognised slice syntax 'first 100'"):
        migrate(k, doc)


def test_nondeterministic_adapter_is_rejected(proj_copy):
    adapter = proj_copy / "migrations/mappings/adapters/structured_slice.py"
    adapter.write_text(adapter.read_text() + "\nimport random\n_orig = migrate\n"
                       "def migrate(doc, ctx):\n    doc = _orig(doc, ctx)\n"
                       "    doc['mappings']['_r'] = random.random()\n    return doc\n")
    k = Project(proj_copy / "schemamig.yaml").kind("tokamap-mappings")
    with pytest.raises(MigrationError, match="not deterministic"):
        migrate(k, load("fixtures/mappings/1.0.0/equilibrium.json"))


def test_config_legacy_unversioned_needs_explicit_source_version(project):
    k = project.kind("tokamap-config")
    doc = load("fixtures/config/1.0.0/mappings.cfg.json")
    with pytest.raises(MigrationError, match="pass --from-version"):
        migrate(k, doc, to="1.2.0")
    out, rep = migrate(k, doc, to="1.2.0", from_version="1.0.0")
    assert out["format_version"] == "1.2.0"
    assert out["metadata"] == {"experiment": "example", "content_version": "1.0.1"}
    assert out["partitions"] == [{"attribute": "shot", "selector": "max_below"}]
    assert [c.path for c in rep.lossy()] == ['$["metadata"]["author"]']


def test_manual_baseline_is_a_schema_baseline_but_never_migrates(project):
    k = project.kind("tokamap-config")
    assert "2.0.0" in k.schemas()
    assert k.chain()[-1].assessment() == {"implementation": "manual-baseline",
                                          "instance_migration": "unsupported",
                                          "compatibility_claim": "unknown"}
    doc = load("fixtures/config/1.2.0/mappings.cfg.json")
    with pytest.raises(MigrationError, match="will not transform documents across it"):
        migrate(k, doc)
    # but stopping at the head before it works
    assert migrate(k, doc, to="1.2.0")[1].noop


def test_impact_report_is_non_destructive(project):
    k = project.kind("tokamap-config")
    paths = sorted((EXAMPLE / "fixtures/config/1.0.0").glob("*.json"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    docs = [(p.name, json.loads(p.read_text())) for p in paths]
    snapshot = copy.deepcopy(docs)
    rep = impact(k, k.edge("1.2.0"), docs, from_version="1.0.0")
    assert docs == snapshot
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    rows = {(r["shape"], r["classification"]) for r in rep["documents"][0]["rows"]}
    assert rows == {("$.partitions", "invalidated"), ("$.metadata", "preserved"), ("$.groups", "preserved")}
    assert rep["documents"][0]["valid_unchanged_under_new_schema"] is False
    assert rep["unused_annotations"] == []


def test_impact_flags_unclassified_paths_and_typos(proj_copy):
    cs = proj_copy / "migrations/config/0003-1.2.0-to-2.0.0.yaml"
    cs.write_text(cs.read_text().replace("  - $.groups\n", "  - $.grups\n"))
    k = Project(proj_copy / "schemamig.yaml").kind("tokamap-config")
    rep = impact(k, k.edge("1.2.0"), [("a", load("fixtures/config/1.2.0/mappings.cfg.json"))])
    assert ("$.groups", "needs-review") in {(r["shape"], r["classification"]) for r in rep["documents"][0]["rows"]}
    assert rep["unused_annotations"] == ["$.grups"]


def test_manual_baseline_with_wrong_id_is_rejected(proj_copy):
    p = proj_copy / "baselines/tokamap-config-2.0.0.json"
    p.write_text(p.read_text().replace("urn:tokamap-config:2.0.0", "urn:tokamap-config:3.0.0"))
    with pytest.raises(SchemaError, match="expected 'urn:tokamap-config:2.0.0'"):
        Project(proj_copy / "schemamig.yaml").kind("tokamap-config").schemas()


def test_manual_baseline_with_attached_adapter_migrates(proj_copy):
    (proj_copy / "migrations/config/adapt.py").write_text(
        "def migrate(doc, ctx):\n"
        "    rules = {}\n"
        "    for i, p in enumerate(doc.pop('partitions')):\n"
        "        rules[p['attribute']] = {'selector': p['selector'], 'priority': i}\n"
        "        ctx.record(('partitions', i), 'converted to partition_rules.' + p['attribute'])\n"
        "    doc['partition_rules'] = rules\n"
        "    return doc\n")
    cs = proj_copy / "migrations/config/0003-1.2.0-to-2.0.0.yaml"
    cs.write_text(cs.read_text().replace("instance_migration: unsupported",
                                         "instance_migration: manual\nadapter: {module: adapt.py, function: migrate}"))
    k = Project(proj_copy / "schemamig.yaml").kind("tokamap-config")
    out, rep = migrate(k, load("fixtures/config/1.0.0/multi-partition.cfg.json"), from_version="1.0.0")
    assert out["partition_rules"] == {"shot": {"selector": "max_below", "priority": 0},
                                      "run": {"selector": "closest", "priority": 1}}
    assert rep.edges[-1].assessment["instance_migration"] == "manual (custom adapter)"


def test_backward_compatible_claim_on_manual_baseline_is_only_warned(proj_copy):
    cs = proj_copy / "migrations/config/0003-1.2.0-to-2.0.0.yaml"
    cs.write_text(cs.read_text() + "compatibility: backward-compatible\n")
    k = Project(proj_copy / "schemamig.yaml").kind("tokamap-config")
    k.schemas()
    assert any("reviewer's assertion only" in w for w in k.warnings)
    assert any("claimed backward-compatible" in p for p in verify(k))  # ...and the fixtures falsify it


def test_editing_a_changeset_makes_build_check_fail(proj_copy, capsys):
    cs = proj_copy / "migrations/mappings/0003-1.1.0-to-2.0.0.yaml"
    cs.write_text(cs.read_text().replace("to: probe", "to: dimension_probe"))
    assert main(["--project", str(proj_copy / "schemamig.yaml"), "build", "--check"]) == 2
    assert "out of date" in capsys.readouterr().out


# --- CLI safety rails ------------------------------------------------------------------------

def test_cli_refuses_in_place_and_overwrite(proj_copy, capsys):
    cfg = str(proj_copy / "schemamig.yaml")
    src = str(proj_copy / "fixtures/mappings/1.0.0/equilibrium.json")
    assert main(["--project", cfg, "migrate", src, "--kind", "tokamap-mappings", "--out", src]) == 1
    assert "in place" in capsys.readouterr().err
    out = str(proj_copy / "out.json")
    assert main(["--project", cfg, "migrate", src, "--kind", "tokamap-mappings", "--out", out]) == 0
    assert main(["--project", cfg, "migrate", src, "--kind", "tokamap-mappings", "--out", out]) == 1
    assert "--force" in capsys.readouterr().err
    # migrating the output again is a reported no-op
    assert main(["--project", cfg, "migrate", out, "--kind", "tokamap-mappings", "--out", out + "2"]) == 0
    assert "already at 2.1.0" in capsys.readouterr().out


def test_cli_explain_and_impact_run(capsys):
    cfg = str(EXAMPLE / "schemamig.yaml")
    assert main(["--project", cfg, "explain", "--kind", "tokamap-mappings"]) == 0
    assert "[adapter" in capsys.readouterr().out
    assert main(["--project", cfg, "impact", str(EXAMPLE / "fixtures/config/1.2.0/mappings.cfg.json"),
                 "--kind", "tokamap-config", "--edge", "1.2.0"]) == 0
    assert "No files were modified" in capsys.readouterr().out

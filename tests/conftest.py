import json
from pathlib import Path

import pytest
import yaml

from schemamig.project import Project

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "tokamap"


@pytest.fixture
def make_project(tmp_path):
    """Build a throwaway one-kind project: make_project(baseline, [changeset, ...]) -> Kind."""

    count = [0]

    def make(baseline: dict, changesets: list[dict], version_path="/v", files: dict | None = None):
        count[0] += 1
        root = tmp_path / f"proj{count[0]}"
        (root / "migrations").mkdir(parents=True)
        baseline = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "urn:t:1.0.0", **baseline}
        (root / "baseline.json").write_text(json.dumps(baseline))
        for i, cs in enumerate(changesets):
            cs = {"document_kind": "t", **cs}
            (root / "migrations" / f"{i:04d}.yaml").write_text(yaml.safe_dump(cs, sort_keys=False))
        for rel, content in (files or {}).items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content if isinstance(content, str) else json.dumps(content))
        (root / "schemamig.yaml").write_text(yaml.safe_dump({"kinds": {"t": {
            "baseline": "baseline.json", "migrations": "migrations", "generated": "generated",
            "fixtures": "fixtures", "version_path": version_path}}}))
        return Project(root / "schemamig.yaml").kind("t")

    return make


# a small schema shaped like libtokamap's: map of discriminated-union entries via $ref
MAPPINGS = {
    "type": "object",
    "properties": {"v": {"type": "string"}},
    "required": ["v"],
    "patternProperties": {"^(?!v$)[a-z_]+$": {"$ref": "#/$defs/entry"}},
    "additionalProperties": False,
    "$defs": {
        "entry": {"type": "object", "oneOf": [{"$ref": "#/$defs/source"}, {"$ref": "#/$defs/value"}]},
        "source": {
            "properties": {"kind": {"const": "SOURCE"}, "source": {"type": "string"},
                           "scale": {"type": ["string", "number"]}, "note": {"type": "string"}},
            "required": ["kind", "source"], "additionalProperties": False},
        "value": {
            "properties": {"kind": {"const": "VALUE"}, "value": {"type": "number"},
                           "unit": {"enum": ["M", "MM"]}},
            "required": ["kind", "value"], "additionalProperties": False},
    },
}


@pytest.fixture
def mappings_schema():
    return json.loads(json.dumps(MAPPINGS))

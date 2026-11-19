"""The record formats as a versioned, validated API.

Journal records (JSON Lines) and release records (YAML) are written by the tool and read
by the CLI, editor integrations and CI, so they get their own JSON Schema. It is
*generated* from the operation classes so the vocabulary and the format cannot drift;
`schemamig record-schema --check` keeps the committed copy in sync.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import ops as O

RECORD_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "record.schema.json"

STR = {"type": "string"}
_KEY_TYPES: dict[str, dict] = {
    "at": STR, "name": STR, "from": STR, "to": STR, "into": STR, "type": STR, "reason": STR,
    "module": STR, "function": STR, "description": STR, "ref": STR,
    "required": {"type": "boolean"}, "lossy": {"type": "boolean"},
    "managed": {"const": "version"}, "position": {"enum": ["start", "end"]},
    "convert": {"enum": sorted(O.CONVERTERS)}, "instance_effect": {"const": "none"},
    "fields": {"type": "array", "items": STR, "minItems": 1},
    "values": {"type": "object", "minProperties": 1},
    "schema": {"type": ["object", "boolean"]}, "default": {},
    "patch": {"$ref": "#/$defs/json_patch"}, "schema_patch": {"$ref": "#/$defs/json_patch"},
    "note": STR,
}
COMPAT = {"enum": ["backward-compatible", "breaking", "unknown"]}


def _op_schema(cls: type[O.Operation]) -> dict:
    keys = sorted(cls.required_keys | cls.optional_keys | {"note"})
    missing = [k for k in keys if k not in _KEY_TYPES]
    assert not missing, f"{cls.op}: add types for {missing}"
    return {"type": "object",
            "properties": {"op": {"const": cls.op}, **{k: _KEY_TYPES[k] for k in keys}},
            "required": ["op", *sorted(cls.required_keys)], "additionalProperties": False}


def build_schema() -> dict:
    from .journal import CLASSES
    ops = {cls.op: _op_schema(cls) for cls in O.OPS.values()}
    common = {"id": {"type": "string", "pattern": "^c[0-9a-f]{8}$"},
              "after": {"type": ["string", "null"]},
              "record_version": {"const": RECORD_VERSION}}
    change_body = {
        "class": {"enum": sorted(CLASSES)}, "target": STR,
        "options": {"type": "object", "description": "parameters the developer declared explicitly"},
        "reason": STR, "compatibility": COMPAT,
        "ops": {"type": "array", "items": {"$ref": "#/$defs/operation"}, "minItems": 1,
                "description": "full operations, declared + inferred parameters"},
    }
    body_req = ["class", "target", "options", "reason", "compatibility", "ops"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:schemamig:records:{RECORD_VERSION}",
        "description": "schemamig record formats (generated from schemamig/ops.py; do not edit)",
        "$defs": {
            "journal_record": {"oneOf": [
                {"type": "object", "properties": {**common, "type": {"const": "change"}, **change_body},
                 "required": ["id", "after", "record_version", "type", *body_req], "additionalProperties": False},
                {"type": "object", "properties": {**common, "type": {"const": "amend"}, "amends": STR, **change_body},
                 "required": ["id", "after", "record_version", "type", "amends", *body_req],
                 "additionalProperties": False},
                {"type": "object", "properties": {**common, "type": {"const": "drop"}, "drops": STR},
                 "required": ["id", "after", "record_version", "type", "drops"], "additionalProperties": False},
            ]},
            "release_record": {
                "type": "object",
                "properties": {
                    "record_version": {"const": RECORD_VERSION},
                    "from": STR, "to": STR, "document_kind": STR, "description": STR,
                    "compatibility": COMPAT, "version_path": {"type": ["string", "null"]},
                    "changes": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"id": STR, "class": {"enum": sorted(CLASSES)}, "target": STR,
                                       "reason": STR, "compatibility": COMPAT, "options": {"type": "object"},
                                       "operations": {"type": "integer", "minimum": 1,
                                                      "description": "how many of `operations` this change contributed"}},
                        "required": ["id", "class", "target", "reason", "compatibility"],
                        "additionalProperties": False}},
                    "schema_snapshot": STR,
                    "operations": {"type": "array", "items": {"$ref": "#/$defs/operation"}, "minItems": 1},
                },
                "required": ["record_version", "from", "to", "document_kind", "compatibility", "changes",
                             "schema_snapshot", "operations"],
                "additionalProperties": False},
            "operation": {"oneOf": [{"$ref": f"#/$defs/op_{n.replace('-', '_')}"} for n in sorted(ops)]},
            **{f"op_{n.replace('-', '_')}": s for n, s in sorted(ops.items())},
            "json_patch": {"type": "array", "items": {
                "type": "object", "required": ["op", "path"],
                "properties": {"op": {"enum": ["add", "remove", "replace", "move", "copy", "test"]},
                               "path": STR, "from": STR, "value": {}}}},
        },
    }


_validators: dict[str, Draft202012Validator] = {}


def validate(kind: str, record: Any) -> list[str]:
    """kind: 'journal_record' | 'release_record'. Returns error strings."""
    if kind not in _validators:
        import json
        schema = json.loads(SCHEMA_PATH.read_text()) if SCHEMA_PATH.exists() else build_schema()
        _validators[kind] = Draft202012Validator({**schema, "$ref": f"#/$defs/{kind}"})
    v = _validators[kind]
    out = []
    for e in sorted(v.iter_errors(record), key=lambda e: list(map(str, e.absolute_path))):
        leaf = min(e.context, key=lambda c: len(c.absolute_path) * -1) if e.context else e
        where = "/".join(map(str, leaf.absolute_path)) or "(record)"
        out.append(f"{where}: {leaf.message[:200]}")
    return out[:5]

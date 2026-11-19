"""The supported JSON Schema subset, $ref resolution, validation and the
schema-aware path resolution used to address operations."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from . import pointer as P


class SchemaError(Exception):
    """Schema is malformed, unsupported, or cannot be addressed as asked."""


DIALECT = "https://json-schema.org/draft/2020-12/schema"

# Keywords whose values are subschemas the engine understands structurally.
STRUCTURAL = {"properties", "patternProperties", "additionalProperties", "items",
              "$ref", "$defs", "oneOf", "anyOf"}
# Keywords treated as opaque leaf constraints/annotations (copied verbatim).
OPAQUE = {"$schema", "$id", "$comment", "title", "description", "default", "examples",
          "type", "enum", "const", "required", "pattern", "format", "minimum", "maximum",
          "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems",
          "maxItems", "uniqueItems", "minProperties", "maxProperties", "multipleOf",
          "deprecated", "readOnly", "writeOnly"}
WHY_UNSUPPORTED = {
    "allOf": "intersections make field ownership ambiguous (which branch owns a renamed field?)",
    "not": "negation cannot be rewritten field-by-field",
    "if": "conditional schemas change which fields apply based on data",
    "then": "conditional schemas change which fields apply based on data",
    "else": "conditional schemas change which fields apply based on data",
    "dependentSchemas": "conditional schemas change which fields apply based on data",
    "dependentRequired": "cross-field requirements are not tracked by rename/remove",
    "prefixItems": "tuple-typed arrays are not addressable by the path language",
    "contains": "'contains' constraints are not tracked",
    "unevaluatedProperties": "depends on evaluation across applicators",
    "unevaluatedItems": "depends on evaluation across applicators",
    "propertyNames": "key constraints are not tracked by rename",
    "$dynamicRef": "dynamic scoping is not supported",
    "$anchor": "only '#/$defs/...' JSON-pointer refs are supported",
    "definitions": "use '$defs' (draft 2020-12)",
}
ALL_KEYWORDS = STRUCTURAL | OPAQUE | set(WHY_UNSUPPORTED)


def check_supported(schema: Any) -> list[str]:
    """Raise SchemaError on unsupported constructs; return lint warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    def visit(node: Any, ptr: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            errors.append(f"{ptr}: subschema must be an object or boolean")
            return
        for kw, val in node.items():
            here = P.child(ptr, kw)
            if kw in WHY_UNSUPPORTED:
                errors.append(f"{here}: '{kw}' is not supported by this tool: {WHY_UNSUPPORTED[kw]}. "
                              "Use a manual-baseline edge for schemas that need it.")
            elif kw.startswith("x-"):
                continue
            elif kw not in STRUCTURAL and kw not in OPAQUE:
                errors.append(f"{here}: unknown keyword '{kw}' (a typo, or a construct this tool does not model)")
        for kw in ("properties", "patternProperties", "$defs"):
            for name, sub in (node.get(kw) or {}).items():
                if kw != "$defs" and name in ALL_KEYWORDS and isinstance(sub, bool):
                    warnings.append(
                        f"{P.child(ptr, kw, name)}: entry named like the keyword '{name}' with a boolean "
                        f"schema; probably meant as a keyword one level up")
                if kw == "patternProperties":
                    try:
                        re.compile(name)
                    except re.error as e:
                        errors.append(f"{P.child(ptr, kw, name)}: invalid regex: {e}")
                visit(sub, P.child(ptr, kw, name))
        for kw in ("additionalProperties", "items"):
            if kw in node:
                if kw == "items" and isinstance(node[kw], list):
                    errors.append(f"{P.child(ptr, kw)}: array-form 'items' is not supported")
                else:
                    visit(node[kw], P.child(ptr, kw))
        for kw in ("oneOf", "anyOf"):
            for i, sub in enumerate(node.get(kw) or []):
                visit(sub, P.child(ptr, kw, str(i)))
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                errors.append(f"{P.child(ptr, '$ref')}: only local '#/$defs/<name>' refs are supported, got {ref!r}")
            elif not P.exists(schema, ref):
                errors.append(f"{P.child(ptr, '$ref')}: dangling ref {ref!r}")
            if len(set(node) - {"$ref", "description", "$comment", "title"}) > 0:
                warnings.append(f"{ptr}: '$ref' with sibling constraints; operations target the referenced schema only")

    visit(schema, "#")
    if errors:
        raise SchemaError("unsupported schema:\n  " + "\n  ".join(errors))
    return warnings


# --- identity -----------------------------------------------------------------

def schema_id(kind: str, version: str) -> str:
    return f"urn:{kind}:{version}"


def parse_id(sid: str) -> tuple[str, str]:
    m = re.fullmatch(r"urn:([A-Za-z0-9_.\-]+):(\d+\.\d+\.\d+)", sid or "")
    if not m:
        raise SchemaError(f"$id {sid!r} is not of the form urn:<kind>:<x.y.z>")
    return m.group(1), m.group(2)


# --- navigation -----------------------------------------------------------------

def deref(schema: dict, ptr: str, seen: set | None = None) -> str:
    """Follow $ref chains from ptr to the canonical location."""
    seen = seen or set()
    node = P.get(schema, ptr)
    while isinstance(node, dict) and "$ref" in node:
        if ptr in seen:
            raise SchemaError(f"ref cycle at {ptr}")
        seen.add(ptr)
        ptr = node["$ref"]
        node = P.get(schema, ptr)
    return ptr


def ref_users(schema: dict, target: str) -> list[str]:
    out = []

    def visit(node: Any, ptr: str) -> None:
        if isinstance(node, dict):
            if node.get("$ref") == target:
                out.append(ptr)
            for k, v in node.items():
                visit(v, P.child(ptr, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, P.child(ptr, str(i)))

    visit(schema, "#")
    return out


_SEG = re.compile(r"\.(\*|[A-Za-z0-9_\-$]+)|\[\]|\[\"([^\"]+)\"\]")


def resolve_location(schema: dict, at: str) -> str:
    """Resolve an operation target to a canonical schema pointer.

    Accepted forms:
      "#/$defs/x/..."   JSON pointer into the schema
      "def:name"        shorthand for #/$defs/name
      "$.a.*.b[]"       instance-shaped path: '.name' = property, '.*' = one map-key level
                        (patternProperties/additionalProperties), '[]' = array items.
    Instance paths cannot cross oneOf/anyOf: address variants by pointer or def:.
    """
    if at.startswith("def:"):
        ptr = P.join("$defs", at[4:])
    elif at.startswith("#"):
        ptr = at
    elif at.startswith("$"):
        ptr = "#"
        rest = at[1:]
        pos = 0
        while pos < len(rest):
            m = _SEG.match(rest, pos)
            if not m:
                raise SchemaError(f"cannot parse path {at!r} at {rest[pos:]!r}")
            pos = m.end()
            ptr = deref(schema, ptr)
            node = P.get(schema, ptr)
            if isinstance(node, dict) and ("oneOf" in node or "anyOf" in node) and "properties" not in node:
                raise SchemaError(f"path {at!r} crosses a union at {ptr}; address the variant with "
                                  f"'def:<name>' or a '#/...' pointer instead")
            seg = m.group(0)
            if seg == "[]":
                if not isinstance(node, dict) or not isinstance(node.get("items"), dict):
                    raise SchemaError(f"path {at!r}: no 'items' schema at {ptr}")
                ptr = P.child(ptr, "items")
            elif m.group(1) == "*":
                pats = list((node.get("patternProperties") or {}).keys()) if isinstance(node, dict) else []
                addl = node.get("additionalProperties") if isinstance(node, dict) else None
                if len(pats) == 1 and not isinstance(addl, dict):
                    ptr = P.child(ptr, "patternProperties", pats[0])
                elif not pats and isinstance(addl, dict):
                    ptr = P.child(ptr, "additionalProperties")
                else:
                    raise SchemaError(f"path {at!r}: '*' at {ptr} needs exactly one patternProperties "
                                      f"entry or an additionalProperties schema (found {pats})")
            else:
                name = m.group(1) or m.group(2)
                props = node.get("properties", {}) if isinstance(node, dict) else {}
                if name not in props:
                    raise SchemaError(f"path {at!r}: no property {name!r} at {ptr}")
                ptr = P.child(ptr, "properties", name)
    else:
        raise SchemaError(f"target {at!r}: expected '#/...', 'def:<name>' or '$.path'")
    if not P.exists(schema, ptr):
        raise SchemaError(f"target {at!r} -> {ptr} does not exist in the schema")
    return deref(schema, ptr)


# --- validation -------------------------------------------------------------------

def validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema)


def check_metaschema(schema: dict) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as e:  # jsonschema.SchemaError
        raise SchemaError(f"not a valid draft 2020-12 schema: {getattr(e, 'message', e)}") from e


@dataclass
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def errors(schema: dict, instance: Any, limit: int = 20) -> list[Problem]:
    v = validator(schema)
    out = []
    for e in sorted(v.iter_errors(instance), key=lambda e: list(map(str, e.absolute_path))):
        # oneOf failures are noisy: report the most relevant sub-error
        leaf = best_match([e]) if e.context else e
        out.append(Problem(P.fmt_path(tuple(e.absolute_path)), leaf.message[:300]))
        if len(out) >= limit:
            break
    return out


def is_valid_at(root_validator: Draft202012Validator, schema: dict, ptr: str, instance: Any) -> bool:
    sub = P.get(schema, ptr)
    return root_validator.evolve(schema=sub).is_valid(instance)


def relaxed(schema: dict, required_to_drop: dict[str, set[str]]) -> dict:
    """Copy of schema with some names removed from 'required' at given pointers."""
    s = copy.deepcopy(schema)
    for ptr, names in required_to_drop.items():
        node = P.get(s, ptr)
        if isinstance(node, dict) and "required" in node:
            node["required"] = [r for r in node["required"] if r not in names]
    return s


def iter_subschemas(schema: Any, ptr: str = "#") -> Iterator[tuple[str, Any]]:
    yield ptr, schema
    if not isinstance(schema, dict):
        return
    for kw in ("properties", "patternProperties", "$defs"):
        for k, v in (schema.get(kw) or {}).items():
            yield from iter_subschemas(v, P.child(ptr, kw, k))
    for kw in ("additionalProperties", "items"):
        if isinstance(schema.get(kw), (dict, bool)) and kw in schema:
            yield from iter_subschemas(schema[kw], P.child(ptr, kw))
    for kw in ("oneOf", "anyOf"):
        for i, v in enumerate(schema.get(kw) or []):
            yield from iter_subschemas(v, P.child(ptr, kw, str(i)))

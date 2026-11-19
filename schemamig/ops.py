"""The operation vocabulary.

Each operation is authored once and interpreted twice:
  * apply_schema(schema)              -> new schema
  * apply_instance(schema_before, doc) -> new doc   (derived from the same spec)

Node-level operations are anchored at a *schema location*; the instance
transformation is applied to every instance node that the walker reports as
governed by that location (deepest first).
"""
from __future__ import annotations

import copy
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from . import jsonpatch
from . import pointer as P
from .schema import SchemaError, deref, ref_users, resolve_location
from .walk import walk


class DslError(Exception):
    """The changeset is malformed or asks for something the tool refuses to do."""


class MigrationError(Exception):
    """An instance cannot be migrated (conflict, unapproved loss, failed conversion...)."""


@dataclass
class Change:
    op: str
    path: str
    detail: str
    lossy: bool = False

    def __str__(self) -> str:
        return f"{'LOSSY ' if self.lossy else ''}{self.op} {self.path}: {self.detail}"


@dataclass
class Ctx:
    from_version: str
    to_version: str
    base_dir: Path
    changes: list[Change] = field(default_factory=list)

    def record(self, op: str, path: tuple, detail: str, lossy: bool = False) -> None:
        self.changes.append(Change(op, P.fmt_path(path), detail, lossy))


# effect categories for old instances
NONE, VALIDATED, REWRITE, LOSSY, ADAPTER = "none", "validated-only", "rewrite", "lossy", "adapter"


# --- helpers ------------------------------------------------------------------

def rename_key(d: dict, old: str, new: str) -> dict:
    return {(new if k == old else k): v for k, v in d.items()}


def insert_at(d: dict, key: str, value: Any, index: int) -> dict:
    items = [(k, v) for k, v in d.items() if k != key]
    items.insert(min(index, len(items)), (key, value))
    return dict(items)


def _validate_value(schema_root: dict, sub: Any, value: Any, what: str) -> None:
    v = Draft202012Validator(schema_root).evolve(schema=sub)
    errs = list(v.iter_errors(value))
    if errs:
        raise DslError(f"{what} {value!r} is not valid for its schema: {errs[0].message}")


def _owner(schema: dict, at: str) -> tuple[str, dict]:
    ptr = resolve_location(schema, at)
    node = P.get(schema, ptr)
    if not isinstance(node, dict):
        raise DslError(f"target {at!r} ({ptr}) is a boolean schema, not an object schema")
    if ("oneOf" in node or "anyOf" in node) and "properties" not in node:
        raise DslError(f"target {at!r} ({ptr}) is a union; target one variant (e.g. def:<variant>)")
    t = node.get("type")
    if t is not None and t != "object" and not (isinstance(t, list) and "object" in t):
        raise DslError(f"target {at!r} ({ptr}) has type {t!r}, not object")
    return ptr, node


# --- base -----------------------------------------------------------------------

class Operation:
    op: str = ""
    required_keys: set[str] = set()
    optional_keys: set[str] = set()
    effect: str = NONE
    escape_hatch = False

    def __init__(self, spec: dict, base_dir: Path):
        unknown = set(spec) - self.required_keys - self.optional_keys - {"op", "note"}
        missing = self.required_keys - set(spec)
        if unknown:
            raise DslError(f"{self.op}: unknown key(s) {sorted(unknown)}")
        if missing:
            raise DslError(f"{self.op}: missing key(s) {sorted(missing)}")
        self.spec = spec
        self.base_dir = base_dir
        self.notes: list[str] = []
        self.validate_spec()

    def validate_spec(self) -> None:
        pass

    def describe(self) -> str:
        return self.op

    def instance_effect(self) -> str:
        return self.effect

    # schema side
    def apply_schema(self, schema: dict) -> dict:
        raise NotImplementedError

    # instance side: default = node transform at target location
    def target(self, schema: dict) -> str | None:
        return None

    def transform(self, value: Any, path: tuple, ctx: Ctx) -> Any:
        return value

    def apply_instance(self, schema_before: dict, doc: Any, ctx: Ctx) -> Any:
        tgt = self.target(schema_before)
        if tgt is None:
            return doc
        doc = copy.deepcopy(doc)
        nodes = [n for n in walk(schema_before, doc) if tgt in n.governing]
        for n in sorted(nodes, key=lambda n: -len(n.path)):
            value = P.get_path(doc, n.path)
            doc = P.set_path(doc, n.path, self.transform(value, n.path, ctx))
        return doc

    def _open_note(self, node: dict, ptr: str, name: str) -> None:
        if node.get("additionalProperties", True) is not False and not node.get("patternProperties"):
            self.notes.append(f"{ptr} is an open object (no additionalProperties: false): an unmigrated "
                              f"document still carrying {name!r} would pass validation under the new schema")

    def _shared_note(self, schema: dict, ptr: str) -> None:
        if ptr.startswith("#/$defs/"):
            users = ref_users(schema, P.join(*P.split(ptr)[:2]))
            if len(users) > 1:
                self.notes.append(f"{ptr} is shared by {len(users)} $ref users: {users}; all are affected")


class FieldOp(Operation):
    """Operations on a named property of an object schema."""

    def target(self, schema: dict) -> str:
        return _owner(schema, self.spec["at"])[0]


# --- declarative operations -----------------------------------------------------------

class AddField(FieldOp):
    op = "add-field"
    required_keys = {"at", "name"}
    optional_keys = {"type", "schema", "required", "default", "description", "managed", "position"}

    def validate_spec(self):
        s = self.spec
        if ("type" in s) == ("schema" in s):
            raise DslError("add-field: give exactly one of 'type' or 'schema'")
        if s.get("position", "end") not in ("start", "end"):
            raise DslError("add-field: 'position' must be 'start' or 'end'")
        if s.get("managed") not in (None, "version"):
            raise DslError("add-field: 'managed' may only be 'version'")
        if s.get("required") and "default" not in s and not s.get("managed"):
            raise DslError(
                f"add-field {s['name']!r}: required field without a default cannot be migrated "
                "automatically (existing documents lack it). Provide 'default', add it as optional, "
                "or use an 'adapter' operation that computes it.")

    def field_schema(self) -> dict:
        fs = copy.deepcopy(self.spec["schema"]) if "schema" in self.spec else {"type": self.spec["type"]}
        if "description" in self.spec:
            fs = {"description": self.spec["description"], **fs}
        return fs

    def describe(self):
        s = self.spec
        req = "required" if s.get("required") else "optional"
        extra = f", default {s['default']!r}" if "default" in s else ""
        extra += ", value managed by tool (document version)" if s.get("managed") else ""
        return f"add {req} field {s['name']!r} to {s['at']}{extra}"

    def instance_effect(self):
        return REWRITE if self.spec.get("required") and "default" in self.spec else NONE

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        self._shared_note(schema, ptr)
        name = self.spec["name"]
        if name in node.get("properties", {}):
            raise DslError(f"add-field: {name!r} already exists at {ptr}")
        props = node.setdefault("properties", {})
        node["properties"] = insert_at(props, name, self.field_schema(),
                                       0 if self.spec.get("position") == "start" else len(props))
        if self.spec.get("required"):
            node["required"] = node.get("required", []) + [name]
        if "default" in self.spec:
            _validate_value(schema, node["properties"][name], self.spec["default"], "default")
        return schema

    def transform(self, value, path, ctx):
        name = self.spec["name"]
        if isinstance(value, dict) and self.spec.get("required") and "default" in self.spec and name not in value:
            ctx.record(self.op, path + (name,), f"inserted default {self.spec['default']!r}")
            idx = 0 if self.spec.get("position") == "start" else len(value)
            return insert_at(value, name, copy.deepcopy(self.spec["default"]), idx)
        return value


class RenameField(FieldOp):
    op = "rename-field"
    required_keys = {"at", "from", "to"}

    def describe(self):
        return f"rename field {self.spec['from']!r} -> {self.spec['to']!r} at {self.spec['at']}"

    effect = REWRITE

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        self._shared_note(schema, ptr)
        a, b = self.spec["from"], self.spec["to"]
        self._open_note(node, ptr, a)
        props = node.get("properties", {})
        if a not in props:
            raise DslError(f"rename-field: {a!r} is not a property at {ptr}")
        if b in props:
            raise DslError(f"rename-field: {b!r} already exists at {ptr}")
        node["properties"] = rename_key(props, a, b)
        if "required" in node:
            node["required"] = [b if r == a else r for r in node["required"]]
        return schema

    def transform(self, value, path, ctx):
        a, b = self.spec["from"], self.spec["to"]
        if not isinstance(value, dict) or a not in value:
            return value  # policy: absent source is a no-op
        if b in value:
            raise MigrationError(f"rename-field conflict at {P.fmt_path(path)}: both {a!r} and {b!r} present; "
                                 "refusing to overwrite. Resolve by hand, then re-run.")
        ctx.record(self.op, path, f"{a!r} -> {b!r}")
        return rename_key(value, a, b)


class RemoveField(FieldOp):
    op = "remove-field"
    required_keys = {"at", "name"}
    optional_keys = {"lossy"}

    def describe(self):
        return f"remove field {self.spec['name']!r} at {self.spec['at']}" + \
            (" (lossy, approved)" if self.spec.get("lossy") else " (refuses documents that use it)")

    def instance_effect(self):
        return LOSSY if self.spec.get("lossy") else VALIDATED

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        self._shared_note(schema, ptr)
        name = self.spec["name"]
        self._open_note(node, ptr, name)
        if name not in node.get("properties", {}):
            raise DslError(f"remove-field: {name!r} is not a property at {ptr}")
        del node["properties"][name]
        if "required" in node:
            node["required"] = [r for r in node["required"] if r != name]
            if not node["required"]:
                del node["required"]
        return schema

    def transform(self, value, path, ctx):
        name = self.spec["name"]
        if not isinstance(value, dict) or name not in value:
            return value
        if not self.spec.get("lossy"):
            raise MigrationError(f"remove-field {name!r}: {P.fmt_path(path + (name,))} has a value "
                                 f"({value[name]!r}); set 'lossy: true' on the operation to approve deletion")
        ctx.record(self.op, path + (name,), f"removed value {value[name]!r}", lossy=True)
        return {k: v for k, v in value.items() if k != name}


class MakeRequired(FieldOp):
    op = "make-required"
    required_keys = {"at", "name"}
    optional_keys = {"default"}

    def describe(self):
        return f"make {self.spec['name']!r} required at {self.spec['at']}" + \
            (f", default {self.spec['default']!r}" if "default" in self.spec else "")

    def instance_effect(self):
        return REWRITE if "default" in self.spec else VALIDATED

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        name = self.spec["name"]
        if name not in node.get("properties", {}):
            raise DslError(f"make-required: {name!r} is not a property at {ptr}")
        if name in node.get("required", []):
            raise DslError(f"make-required: {name!r} is already required at {ptr}")
        node["required"] = node.get("required", []) + [name]
        if "default" in self.spec:
            _validate_value(schema, node["properties"][name], self.spec["default"], "default")
        return schema

    def transform(self, value, path, ctx):
        name = self.spec["name"]
        if isinstance(value, dict) and name not in value:
            if "default" not in self.spec:
                raise MigrationError(f"make-required {name!r}: missing at {P.fmt_path(path)} and no default given")
            ctx.record(self.op, path + (name,), f"inserted default {self.spec['default']!r}")
            return {**value, name: copy.deepcopy(self.spec["default"])}
        return value


class MakeOptional(FieldOp):
    op = "make-optional"
    required_keys = {"at", "name"}

    def describe(self):
        return f"make {self.spec['name']!r} optional at {self.spec['at']}"

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        name = self.spec["name"]
        if name not in node.get("required", []):
            raise DslError(f"make-optional: {name!r} is not required at {ptr}")
        node["required"] = [r for r in node["required"] if r != name]
        if not node["required"]:
            del node["required"]
        return schema

    def target(self, schema):
        return None


CONVERTERS: dict[str, Callable[[Any], Any]] = {}


def _to_number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        try:
            f = float(v)
            return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
        except ValueError:
            pass
    raise ValueError(f"cannot convert {v!r} to a number")


CONVERTERS["to-number"] = _to_number
CONVERTERS["to-string"] = lambda v: v if isinstance(v, str) else json_scalar_str(v)
CONVERTERS["to-array"] = lambda v: v if isinstance(v, list) else [v]


def json_scalar_str(v):
    if isinstance(v, (dict, list)):
        raise ValueError(f"cannot convert {type(v).__name__} to string")
    import json
    return json.dumps(v)


class SetFieldSchema(FieldOp):
    op = "set-field-schema"
    required_keys = {"at", "name", "schema"}
    optional_keys = {"convert"}

    def validate_spec(self):
        c = self.spec.get("convert")
        if c is not None and c not in CONVERTERS:
            raise DslError(f"set-field-schema: unknown convert {c!r}; choose from {sorted(CONVERTERS)}")

    def describe(self):
        conv = self.spec.get("convert")
        return f"replace schema of {self.spec['name']!r} at {self.spec['at']}" + \
            (f" (convert values: {conv})" if conv else " (values unchanged; must still validate)")

    def instance_effect(self):
        return REWRITE if self.spec.get("convert") else VALIDATED

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        self._shared_note(schema, ptr)
        name = self.spec["name"]
        if name not in node.get("properties", {}):
            raise DslError(f"set-field-schema: {name!r} is not a property at {ptr}")
        node["properties"][name] = copy.deepcopy(self.spec["schema"])
        return schema

    def transform(self, value, path, ctx):
        name, conv = self.spec["name"], self.spec.get("convert")
        if not conv or not isinstance(value, dict) or name not in value:
            return value
        try:
            new = CONVERTERS[conv](value[name])
        except ValueError as e:
            raise MigrationError(f"set-field-schema at {P.fmt_path(path + (name,))}: {e}") from e
        if new != value[name] or type(new) is not type(value[name]):
            ctx.record(self.op, path + (name,), f"{value[name]!r} -> {new!r} ({conv})")
        return {**value, name: new}


class RenameValue(Operation):
    op = "rename-value"
    required_keys = {"at", "values"}
    optional_keys = {"name"}
    effect = REWRITE

    def validate_spec(self):
        vals = self.spec["values"]
        if not isinstance(vals, dict) or not vals:
            raise DslError("rename-value: 'values' must be a non-empty mapping OLD: NEW")
        if len(set(vals.values())) != len(vals):
            raise DslError("rename-value: several old values map to the same new value (lossy merge)")

    def describe(self):
        where = self.spec["at"] + (f" field {self.spec['name']!r}" if "name" in self.spec else "")
        return f"rename values at {where}: " + ", ".join(f"{a}->{b}" for a, b in self.spec["values"].items())

    def target(self, schema):
        ptr = resolve_location(schema, self.spec["at"])
        if "name" in self.spec:
            node = P.get(schema, ptr)
            if self.spec["name"] not in (node.get("properties") or {}):
                raise DslError(f"rename-value: no property {self.spec['name']!r} at {ptr}")
            ptr = deref(schema, P.child(ptr, "properties", self.spec["name"]))
        return ptr

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr = self.target(schema)
        self._shared_note(schema, ptr)
        node = P.get(schema, ptr)
        m = self.spec["values"]
        if not isinstance(node, dict) or ("enum" not in node and "const" not in node):
            raise DslError(f"rename-value: {ptr} has no 'enum' or 'const' to rename")
        if "enum" in node:
            missing = set(m) - set(node["enum"])
            if missing:
                raise DslError(f"rename-value: {sorted(missing)} not in enum at {ptr}")
            clash = (set(m.values()) & set(node["enum"])) - set(m)
            if clash:
                raise DslError(f"rename-value: new value(s) {sorted(clash)} already in enum at {ptr}")
            node["enum"] = [m.get(v, v) for v in node["enum"]]
        if "const" in node:
            if node["const"] not in m:
                raise DslError(f"rename-value: const {node['const']!r} at {ptr} not in mapping")
            node["const"] = m[node["const"]]
        if node.get("default") in m:
            node["default"] = m[node["default"]]
        return schema

    def transform(self, value, path, ctx):
        m = self.spec["values"]
        if isinstance(value, (str, int, float, bool)) and value in m:
            ctx.record(self.op, path, f"{value!r} -> {m[value]!r}")
            return m[value]
        return value


class AddDef(Operation):
    op = "add-def"
    required_keys = {"name", "schema"}

    def describe(self):
        return f"add $defs/{self.spec['name']}"

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        defs = schema.setdefault("$defs", {})
        if self.spec["name"] in defs:
            raise DslError(f"add-def: $defs/{self.spec['name']} already exists")
        defs[self.spec["name"]] = copy.deepcopy(self.spec["schema"])
        return schema


class AddVariant(Operation):
    op = "add-variant"
    required_keys = {"at"}
    optional_keys = {"ref", "schema"}

    def validate_spec(self):
        if ("ref" in self.spec) == ("schema" in self.spec):
            raise DslError("add-variant: give exactly one of 'ref' or 'schema'")

    def describe(self):
        return f"add union variant {self.spec.get('ref') or '(inline schema)'} to {self.spec['at']}"

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr = resolve_location(schema, self.spec["at"])
        node = P.get(schema, ptr)
        kw = "oneOf" if "oneOf" in node else "anyOf" if "anyOf" in node else None
        if kw is None:
            raise DslError(f"add-variant: {ptr} has no oneOf/anyOf")
        ref = self.spec.get("ref")
        if ref:
            if ref.startswith("def:"):
                ref = P.join("$defs", ref[4:])
            if not P.exists(schema, ref):
                raise DslError(f"add-variant: {ref} does not exist (add-def it first)")
            node[kw].append({"$ref": ref})
        else:
            node[kw].append(copy.deepcopy(self.spec["schema"]))
        return schema


class NestFields(FieldOp):
    op = "nest-fields"
    required_keys = {"at", "fields", "into"}
    optional_keys = {"description"}
    effect = REWRITE

    def describe(self):
        return f"move fields {self.spec['fields']} at {self.spec['at']} into new object {self.spec['into']!r}"

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr, node = _owner(schema, self.spec["at"])
        self._shared_note(schema, ptr)
        fields, into = self.spec["fields"], self.spec["into"]
        props = node.get("properties", {})
        for f in fields:
            if f not in props:
                raise DslError(f"nest-fields: {f!r} is not a property at {ptr}")
        if into in props:
            raise DslError(f"nest-fields: {into!r} already exists at {ptr}")
        req = node.get("required", [])
        sub: dict = {"type": "object"}
        if "description" in self.spec:
            sub = {"description": self.spec["description"], **sub}
        sub["properties"] = {f: props[f] for f in fields}
        moved_req = [f for f in fields if f in req]
        if moved_req:
            sub["required"] = moved_req
        sub["additionalProperties"] = False
        first = min(list(props).index(f) for f in fields)
        rest = {k: v for k, v in props.items() if k not in fields}
        node["properties"] = insert_at(rest, into, sub, first)
        if req:
            new_req = [r for r in req if r not in fields] + ([into] if moved_req else [])
            if new_req:
                node["required"] = new_req
            else:
                del node["required"]
        return schema

    def transform(self, value, path, ctx):
        fields, into = self.spec["fields"], self.spec["into"]
        if not isinstance(value, dict):
            return value
        present = [f for f in fields if f in value]
        if not present:
            return value
        if into in value:
            raise MigrationError(f"nest-fields conflict at {P.fmt_path(path)}: {into!r} already present")
        first = min(list(value).index(f) for f in present)
        sub = {f: value[f] for f in fields if f in value}
        rest = {k: v for k, v in value.items() if k not in fields}
        ctx.record(self.op, path, f"moved {present} into {into!r}")
        return insert_at(rest, into, sub, first)


HOIST = ("$schema", "$id", "title", "description", "$comment", "$defs")


class Wrap(Operation):
    op = "wrap"
    required_keys = {"at", "into"}
    effect = REWRITE

    def describe(self):
        return f"wrap {self.spec['at']} as the value of a new property {self.spec['into']!r}"

    def target(self, schema):
        return resolve_location(schema, self.spec["at"])

    def apply_schema(self, schema):
        schema = copy.deepcopy(schema)
        ptr = self.target(schema)
        into = self.spec["into"]
        for user_ptr, sub in _all_refs(schema):
            if sub.startswith(ptr + "/") and ptr != "#":
                raise DslError(f"wrap: {user_ptr} refers inside {ptr}; refs would break")
        if ptr == "#":
            hoisted = {k: schema[k] for k in HOIST if k in schema}
            inner = {k: v for k, v in schema.items() if k not in HOIST}
            return {**{k: v for k, v in hoisted.items() if k != "$defs"},
                    "type": "object", "properties": {into: inner}, "required": [into],
                    "additionalProperties": False,
                    **({"$defs": hoisted["$defs"]} if "$defs" in hoisted else {})}
        toks = P.split(ptr)
        parent = P.get(schema, P.join(*toks[:-1]))
        key = int(toks[-1]) if isinstance(parent, list) else toks[-1]
        parent[key] = {"type": "object", "properties": {into: parent[key]}, "required": [into],
                       "additionalProperties": False}
        return schema

    def transform(self, value, path, ctx):
        ctx.record(self.op, path, f"wrapped under {self.spec['into']!r}")
        return {self.spec["into"]: value}


def _all_refs(schema):
    out = []

    def visit(n, p):
        if isinstance(n, dict):
            if isinstance(n.get("$ref"), str):
                out.append((p, n["$ref"]))
            for k, v in n.items():
                visit(v, P.child(p, k))
        elif isinstance(n, list):
            for i, v in enumerate(n):
                visit(v, P.child(p, str(i)))
    visit(schema, "#")
    return out


# --- escape hatches -----------------------------------------------------------------

class PatchSchema(Operation):
    op = "patch-schema"
    required_keys = {"patch", "reason"}
    optional_keys = {"instance_effect"}
    escape_hatch = True

    def validate_spec(self):
        if self.spec.get("instance_effect", "none") != "none":
            raise DslError("patch-schema: instance_effect must be 'none' (use an 'adapter' op to rewrite documents)")

    def describe(self):
        return f"raw schema patch ({len(self.spec['patch'])} ops): {self.spec['reason']}"

    effect = VALIDATED

    def apply_schema(self, schema):
        try:
            return jsonpatch.apply(schema, self.spec["patch"])
        except jsonpatch.PatchError as e:
            raise DslError(f"patch-schema: {e}") from e


class AdapterContext:
    def __init__(self, ctx: Ctx, name: str):
        self._ctx, self._name = ctx, name
        self.from_version, self.to_version = ctx.from_version, ctx.to_version

    def record(self, path: tuple, detail: str, lossy: bool = False) -> None:
        self._ctx.record(self._name, tuple(path), detail, lossy)

    def fail(self, path: tuple, message: str):
        raise MigrationError(f"adapter {self._name} at {P.fmt_path(tuple(path))}: {message}")


class Adapter(Operation):
    op = "adapter"
    required_keys = {"module", "function", "reason"}
    optional_keys = {"schema_patch"}
    effect = ADAPTER
    escape_hatch = True

    def validate_spec(self):
        p = (self.base_dir / self.spec["module"]).resolve()
        if not p.is_file():
            raise DslError(f"adapter: module {p} not found")
        self.module_path = p

    def describe(self):
        return f"custom adapter {self.spec['module']}:{self.spec['function']}: {self.spec['reason']}"

    def apply_schema(self, schema):
        if not self.spec.get("schema_patch"):
            return copy.deepcopy(schema)
        try:
            return jsonpatch.apply(schema, self.spec["schema_patch"])
        except jsonpatch.PatchError as e:
            raise DslError(f"adapter schema_patch: {e}") from e

    def _fn(self):
        spec = importlib.util.spec_from_file_location(f"_adapter_{self.module_path.stem}", self.module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, self.spec["function"])

    def apply_instance(self, schema_before, doc, ctx):
        fn = self._fn()
        name = f"{self.spec['module']}:{self.spec['function']}"
        first = fn(copy.deepcopy(doc), AdapterContext(ctx, name))
        scratch = Ctx(ctx.from_version, ctx.to_version, ctx.base_dir)
        second = fn(copy.deepcopy(doc), AdapterContext(scratch, name))
        if first != second:
            raise MigrationError(f"adapter {name} is not deterministic (two runs disagree)")
        return first


OPS: dict[str, type[Operation]] = {c.op: c for c in [
    AddField, RenameField, RemoveField, MakeRequired, MakeOptional, SetFieldSchema,
    RenameValue, AddDef, AddVariant, NestFields, Wrap, PatchSchema, Adapter]}


def parse_op(spec: dict, base_dir: Path) -> Operation:
    if not isinstance(spec, dict) or "op" not in spec:
        raise DslError(f"operation must be a mapping with an 'op' key: {spec!r}")
    cls = OPS.get(spec["op"])
    if cls is None:
        raise DslError(f"unknown operation {spec['op']!r}; supported: {sorted(OPS)}")
    return cls(spec, base_dir)

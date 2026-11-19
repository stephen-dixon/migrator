"""Schema comparison for the journal workflow.

`normalize` erases differences that cannot affect validation (annotations, key order,
order of set-like lists), so prose and layout edits never need a change record.
`residual` lists the remaining (validation-relevant) differences as pointers.
`patch` produces an RFC 6902 patch turning one raw schema into another (used to record
unclassified changes through the patch-schema escape hatch).
"""
from __future__ import annotations

import json
from typing import Any

from . import pointer as P

ANNOTATIONS = {"title", "description", "$comment", "examples", "deprecated", "readOnly",
               "writeOnly", "$id"}
NAME_MAPS = {"properties", "patternProperties", "$defs"}
SUBSCHEMA = {"additionalProperties", "items"}
SUBSCHEMA_LISTS = {"oneOf", "anyOf"}


def _key(x: Any) -> str:
    return json.dumps(x, sort_keys=True)


def normalize(s: Any) -> Any:
    """Normalize a schema node (not a name map)."""
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k in ANNOTATIONS or k.startswith("x-"):
            continue
        if k in NAME_MAPS and isinstance(v, dict):
            out[k] = {name: normalize(sub) for name, sub in v.items()}
        elif k in SUBSCHEMA:
            out[k] = normalize(v)
        elif k in SUBSCHEMA_LISTS and isinstance(v, list):
            out[k] = sorted((normalize(b) for b in v), key=_key)
        elif k in ("required", "enum") and isinstance(v, list):
            out[k] = sorted(v, key=_key)
        else:
            out[k] = v
    return out


def equivalent(a: Any, b: Any) -> bool:
    return _key(normalize(a)) == _key(normalize(b))


def residual_items(a: Any, b: Any, limit: int = 50) -> list[tuple[str, str]]:
    """(pointer, message) for each validation-relevant difference: a expected, b actual."""
    out: list[tuple[str, str]] = []

    def walk(x: Any, y: Any, ptr: str) -> None:
        if len(out) >= limit or _key(x) == _key(y):
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in x:
                if k not in y:
                    out.append((P.child(ptr, k), "in expected, missing from working schema"))
            for k in y:
                if k not in x:
                    out.append((P.child(ptr, k), "in working schema, not explained by any change"))
            for k in x:
                if k in y:
                    walk(x[k], y[k], P.child(ptr, k))
        else:
            out.append((ptr, f"expected {_short(x)}, working schema has {_short(y)}"))

    walk(normalize(a), normalize(b), "#")
    return out


def residual(a: Any, b: Any, limit: int = 12) -> list[str]:
    return [f"{p}: {m}" for p, m in residual_items(a, b, limit)]


def _short(v: Any) -> str:
    t = json.dumps(v)
    return t if len(t) <= 60 else t[:57] + "..."


def patch(a: Any, b: Any) -> list[dict]:
    ops: list[dict] = []

    def walk(x: Any, y: Any, toks: list[str]) -> None:
        if _key(x) == _key(y):
            return
        path = "".join("/" + P.escape(t) for t in toks)
        if isinstance(x, dict) and isinstance(y, dict) and toks:
            for k in x:
                if k not in y:
                    ops.append({"op": "remove", "path": path + "/" + P.escape(k)})
            for k in y:
                if k not in x:
                    ops.append({"op": "add", "path": path + "/" + P.escape(k), "value": y[k]})
                else:
                    walk(x[k], y[k], toks + [k])
        elif isinstance(x, dict) and isinstance(y, dict):  # root: never replace whole document
            for k in x:
                if k not in y:
                    ops.append({"op": "remove", "path": "/" + P.escape(k)})
            for k in y:
                if k not in x:
                    ops.append({"op": "add", "path": "/" + P.escape(k), "value": y[k]})
                else:
                    walk(x[k], y[k], [k])
        else:
            ops.append({"op": "replace", "path": path, "value": y})

    walk(a, b, [])
    return ops


def touched(a: Any, b: Any) -> set[str]:
    """Pointers of validation-relevant differences (coarse: deepest differing nodes)."""
    out: set[str] = set()

    def walk(x, y, ptr):
        if _key(x) == _key(y):
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in set(x) | set(y):
                if k in x and k in y:
                    walk(x[k], y[k], P.child(ptr, k))
                else:
                    out.add(P.child(ptr, k))
        else:
            out.add(ptr)

    walk(normalize(a), normalize(b), "#")
    return out


def overlaps(a: set[str], b: set[str]) -> bool:
    def pre(x, y):
        tx, ty = P.split(x), P.split(y)
        return tx[:len(ty)] == ty or ty[:len(tx)] == tx
    return any(pre(x, y) for x in a for y in b)

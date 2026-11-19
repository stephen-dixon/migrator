"""Co-walk an instance with its schema, recording which schema locations
govern each instance node.

This is what lets one authored operation (anchored at a *schema* location)
drive the instance transformation: every instance node governed by the
operation's target location gets transformed.

Union handling: for oneOf/anyOf the governing branches are those the node
validates against. Under oneOf (on a valid instance) that is exactly one.
Under anyOf, or overlapping patternProperties, a node may be governed by
several locations at once, which is reported as ambiguity by callers that
care.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import pointer as P
from .schema import is_valid_at, validator


@dataclass
class Node:
    path: tuple                 # concrete instance path
    shape: tuple                # generalised path: map keys -> "*", indices -> "[]"
    governing: set[str] = field(default_factory=set)


def walk(schema: dict, instance: Any) -> list[Node]:
    v = validator(schema)
    out: list[Node] = []

    def expand(ptr: str, value: Any, acc: set[str], depth: int = 0) -> None:
        if depth > 64:
            raise RuntimeError(f"schema recursion too deep at {ptr}")
        acc.add(ptr)
        node = P.get(schema, ptr)
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            expand(node["$ref"], value, acc, depth + 1)
        for kw in ("oneOf", "anyOf"):
            for i, _ in enumerate(node.get(kw) or []):
                sub = P.child(ptr, kw, str(i))
                if is_valid_at(v, schema, sub, value):
                    expand(sub, value, acc, depth + 1)

    def visit(value: Any, path: tuple, shape: tuple, ptrs: set[str]) -> None:
        gov: set[str] = set()
        for p in ptrs:
            expand(p, value, gov)
        out.append(Node(path, shape, gov))
        if isinstance(value, dict):
            for key, child in value.items():
                cptrs: set[str] = set()
                by_prop = by_map = False
                for g in gov:
                    s = P.get(schema, g)
                    if not isinstance(s, dict):
                        continue
                    # JSON Schema semantics: properties AND matching patternProperties both
                    # apply; additionalProperties only if neither matched.
                    hit = False
                    if key in (s.get("properties") or {}):
                        cptrs.add(P.child(g, "properties", key))
                        by_prop = hit = True
                    for pat in (s.get("patternProperties") or {}):
                        if re.search(pat, key):
                            cptrs.add(P.child(g, "patternProperties", pat))
                            by_map = hit = True
                    if not hit and isinstance(s.get("additionalProperties"), dict):
                        cptrs.add(P.child(g, "additionalProperties"))
                        by_map = True
                via_map = by_map and not by_prop
                visit(child, path + (key,), shape + ("*" if via_map else key,), cptrs)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                cptrs = set()
                for g in gov:
                    s = P.get(schema, g)
                    if isinstance(s, dict) and isinstance(s.get("items"), dict):
                        cptrs.add(P.child(g, "items"))
                visit(child, path + (i,), shape + ("[]",), cptrs)

    visit(instance, (), (), {"#"})
    return out


def fmt_shape(shape: tuple) -> str:
    out = "$"
    for s in shape:
        out += "[]" if s == "[]" else f".{s}"
    return out

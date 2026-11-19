"""Minimal RFC 6902 JSON Patch, used by the `patch-schema` escape hatch."""
from __future__ import annotations

import copy
from typing import Any

from . import pointer as P


class PatchError(Exception):
    pass


def _parent(doc: Any, path: str) -> tuple[Any, str]:
    toks = P.split(path)
    if not toks:
        raise PatchError("cannot target the document root")
    parent = doc
    for t in toks[:-1]:
        parent = parent[int(t)] if isinstance(parent, list) else parent[t]
    return parent, toks[-1]


def apply(doc: Any, patch: list[dict]) -> Any:
    doc = copy.deepcopy(doc)
    for i, op in enumerate(patch):
        kind, path = op.get("op"), op.get("path")
        try:
            if kind == "test":
                if P.get(doc, path) != op["value"]:
                    raise PatchError(f"test failed at {path}")
                continue
            if kind in ("move", "copy"):
                value = P.get(doc, op["from"])
                if kind == "move":
                    fp, fk = _parent(doc, op["from"])
                    del fp[int(fk) if isinstance(fp, list) else fk]
                kind, op = "add", {**op, "value": copy.deepcopy(value)}
            parent, key = _parent(doc, path)
            if kind == "add":
                if isinstance(parent, list):
                    parent.insert(len(parent) if key == "-" else int(key), op["value"])
                else:
                    parent[key] = op["value"]
            elif kind == "remove":
                del parent[int(key) if isinstance(parent, list) else key]
            elif kind == "replace":
                k = int(key) if isinstance(parent, list) else key
                if not isinstance(parent, list) and k not in parent:
                    raise PatchError(f"replace: {path} does not exist")
                parent[k] = op["value"]
            else:
                raise PatchError(f"unknown patch op {kind!r}")
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise PatchError(f"patch[{i}] {kind} {path}: path not found ({e})") from e
    return doc

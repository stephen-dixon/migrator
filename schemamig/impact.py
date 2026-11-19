"""Non-destructive impact report for manual-baseline edges.

Classifies every part of each document against the edge's reviewed
preserve_paths / invalidate_paths annotations. Nothing is rewritten.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from . import ops as O
from . import pointer as P
from .migrate import detect_version, migrate
from .project import Changeset, Kind
from .schema import SchemaError, errors, is_valid_at, resolve_location, validator
from .walk import fmt_shape, walk

_TOK = re.compile(r"\.(\*|[A-Za-z0-9_\-$]+)|\[\]|\[\"([^\"]+)\"\]")


def parse_pattern(p: str) -> list[str]:
    if not p.startswith("$"):
        raise O.DslError(f"path pattern {p!r} must start with '$'")
    toks, pos, s = [], 0, p[1:]
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m:
            raise O.DslError(f"cannot parse path pattern {p!r} at {s[pos:]!r}")
        toks.append("[]" if m.group(0) == "[]" else (m.group(1) or m.group(2)))
        pos = m.end()
    return toks


def prefix_match(pattern: list[str], path: tuple) -> bool:
    if len(pattern) > len(path):
        return False
    for t, p in zip(pattern, path):
        if t == "[]":
            if not isinstance(p, int):
                return False
        elif t != "*" and t != p:
            return False
    return True


def impact(kind: Kind, edge: Changeset, docs: list[tuple[str, Any]], from_version: str | None = None) -> dict:
    if edge.kind != "manual-baseline":
        raise O.DslError(f"{edge.name} is declarative; use `migrate` (dry run) instead of `impact`")
    schemas = kind.schemas()
    old_s, new_s = schemas[edge.from_], schemas[edge.to]
    pres = [(p, parse_pattern(p)) for p in edge.preserve_paths]
    inval = [(p, parse_pattern(p)) for p in edge.invalidate_paths]
    used: set[str] = set()
    result: dict = {"edge": edge.name, "reason": edge.reason, "instance_migration": edge.instance_migration,
                    "compatibility_claim": edge.compatibility, "documents": []}
    for name, doc in docs:
        src = detect_version(kind, doc, from_version)
        if src != edge.from_:
            doc, _ = migrate(kind, doc, to=edge.from_, from_version=from_version)  # in memory only
        rows: dict[tuple, dict] = defaultdict(lambda: {"count": 0})
        cls_of: dict[tuple, str] = {(): "root"}
        vf = kind.version_field(edge.from_)
        for n in walk(old_s, doc):
            if not n.path:
                continue
            if len(n.path) == 1 and n.path[0] == vf:
                cls_of[n.path] = "version-field"
                continue
            cls, why = "needs-review", None
            for p, toks in inval:
                if prefix_match(toks, n.path):
                    cls, why = "invalidated", p
                    used.add(p)
                    break
            else:
                for p, toks in pres:
                    if prefix_match(toks, n.path):
                        cls, why = "preserved", p
                        used.add(p)
                        break
            cls_of[n.path] = cls
            if cls_of.get(n.path[:-1]) == cls:
                continue  # covered by an ancestor row
            key = (fmt_shape(n.shape), cls)
            row = rows[key]
            row.update(shape=key[0], classification=cls, rule=why)
            row["count"] += 1
            if cls == "preserved" and "in_new_schema" not in row:
                try:
                    ptr = resolve_location(new_s, key[0])
                    ok = is_valid_at(validator(new_s), new_s, ptr, P.get_path(doc, n.path))
                    row["in_new_schema"] = "yes, value valid there" if ok else \
                        "path exists but value is INVALID there (review)"
                except SchemaError as e:
                    row["in_new_schema"] = f"cannot confirm ({str(e).split(';')[0]})"
        stamped = dict(doc) if isinstance(doc, dict) else doc
        nv = kind.version_field(edge.to)
        if isinstance(stamped, dict):
            if vf:
                stamped.pop(vf, None)
            if nv:
                stamped = {nv: edge.to, **stamped}
        errs = errors(new_s, stamped, limit=5)
        result["documents"].append({
            "document": name, "source_version": src, "evaluated_at": edge.from_,
            "valid_unchanged_under_new_schema": not errs,
            "new_schema_errors": [str(e) for e in errs],
            "rows": sorted(rows.values(), key=lambda r: (r["classification"], r["shape"]))})
    result["unused_annotations"] = [p for p, _ in pres + inval if p not in used]
    return result


def render(rep: dict) -> str:
    out = [f"IMPACT REPORT (non-destructive) for manual baseline {rep['edge']}",
           f"  reason: {rep['reason']}",
           f"  instance migration: {rep['instance_migration']}; compatibility claim: {rep['compatibility_claim']}"]
    for d in rep["documents"]:
        via = "" if d["source_version"] == d["evaluated_at"] else \
            f", migrated in memory {d['source_version']} -> {d['evaluated_at']}"
        out.append(f"\n  {d['document']} (at {d['source_version']}{via}):")
        for r in d["rows"]:
            extra = f"  [rule {r['rule']}]" if r.get("rule") else ""
            if "in_new_schema" in r:
                extra += f"  in new schema: {r['in_new_schema']}"
            out.append(f"    {r['classification']:<13} {r['shape']:<40} x{r['count']}{extra}")
        ok = d["valid_unchanged_under_new_schema"]
        out.append(f"    valid unchanged under new schema: {'yes' if ok else 'no'}")
        for e in d["new_schema_errors"]:
            out.append(f"      {e}")
    if rep["unused_annotations"]:
        out.append(f"\n  WARNING: annotations matched nothing (typo?): {rep['unused_annotations']}")
    review = sum(1 for d in rep["documents"] for r in d["rows"] if r["classification"] == "needs-review")
    out.append(f"\n  {review} row(s) need review. No files were modified.")
    return "\n".join(out)

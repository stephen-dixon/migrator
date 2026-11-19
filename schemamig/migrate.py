"""Instance migration along the chain, with validation at every step."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from . import ops as O
from .project import Kind, vkey
from .schema import errors, relaxed


@dataclass
class EdgeReport:
    name: str
    assessment: dict
    changes: list[O.Change] = field(default_factory=list)


@dataclass
class Report:
    kind: str
    source_version: str
    target_version: str
    noop: bool = False
    edges: list[EdgeReport] = field(default_factory=list)

    def lossy(self) -> list[O.Change]:
        return [c for e in self.edges for c in e.changes if c.lossy]

    def render(self) -> str:
        if self.noop:
            return (f"{self.kind}: document is already at {self.target_version}; nothing to do "
                    "(no output written)")
        lines = [f"{self.kind}: {self.source_version} -> {self.target_version}"]
        for e in self.edges:
            a = e.assessment
            lines.append(f"  edge {e.name}  [{a['implementation']}; migration: {a['instance_migration']}; "
                         f"compatibility: {a['compatibility_claim']}]")
            for c in e.changes:
                lines.append(f"    - {c}")
            if not e.changes:
                lines.append("    (no document changes)")
        lost = self.lossy()
        if lost:
            lines.append(f"  WARNING: {len(lost)} value(s) removed (approved as lossy in the changeset)")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {"kind": self.kind, "from": self.source_version, "to": self.target_version, "noop": self.noop,
                "edges": [{"edge": e.name, **e.assessment,
                           "changes": [c.__dict__ for c in e.changes]} for e in self.edges]}


def detect_version(kind: Kind, doc: Any, override: str | None) -> str:
    versions = kind.versions()
    if override:
        if override not in versions:
            raise O.MigrationError(f"unknown source version {override}; known: {versions}")
        f = kind.version_field(override)
        if f and isinstance(doc, dict) and f in doc and doc[f] != override:
            raise O.MigrationError(f"--from-version {override} contradicts document field {f!r}={doc[f]!r}")
        return override
    if not isinstance(doc, dict):
        raise O.MigrationError("document root is not an object; cannot read its version")
    found = [v for v in versions if (f := kind.version_field(v)) and doc.get(f) == v]
    if len(found) == 1:
        return found[0]
    fields = sorted({f for v in versions if (f := kind.version_field(v))})
    present = {f: doc[f] for f in fields if f in doc}
    if present and not found:
        raise O.MigrationError(f"unknown document version {present}; this tool knows {versions}")
    if not found:
        raise O.MigrationError(f"no version field found (looked for {fields}); for legacy/unversioned "
                               "documents pass --from-version explicitly")
    raise O.MigrationError(f"ambiguous version fields {present}")


def migrate(kind: Kind, doc: Any, to: str | None = None, from_version: str | None = None) -> tuple[Any, Report]:
    schemas = kind.schemas()
    src = detect_version(kind, doc, from_version)
    target = to or kind.versions()[-1]
    if target not in schemas:
        raise O.MigrationError(f"unknown target version {target}")
    if vkey(target) < vkey(src):
        raise O.MigrationError(f"downgrades are not supported ({src} -> {target})")
    report = Report(kind.name, src, target)

    pre = errors(schemas[src], doc)
    if pre:
        raise O.MigrationError(f"document is not valid under its declared version {src}:\n  "
                               + "\n  ".join(map(str, pre)))
    if src == target:
        report.noop = True
        return doc, report

    doc = copy.deepcopy(doc)
    cur = src
    for edge in kind.chain():
        if vkey(edge.from_) < vkey(cur) or vkey(edge.from_) >= vkey(target):
            continue
        ctx = O.Ctx(edge.from_, edge.to, edge.file.parent)
        er = EdgeReport(edge.name, edge.assessment())
        report.edges.append(er)
        f_from, f_to = kind.version_field(edge.from_), kind.version_field(edge.to)
        pos = None
        if f_from and isinstance(doc, dict) and f_from in doc:
            pos = list(doc).index(f_from)
            doc = {k: v for k, v in doc.items() if k != f_from}
        version_fields = {f for f in (f_from, f_to) if f}

        if edge.kind == "manual-baseline":
            if edge.adapter is None:
                raise O.MigrationError(
                    f"edge {edge.name} is a manual baseline with instance_migration: "
                    f"{edge.instance_migration}. The tool will not transform documents across it. "
                    f"Reason given: {edge.reason!r}. Use `schemamig impact` for a review report, "
                    "migrate by hand, or attach a reviewed adapter to the edge.")
            doc = edge.adapter.apply_instance(schemas[edge.from_], doc, ctx)
        else:
            schema = schemas[edge.from_]
            for i, op in enumerate(edge.operations):
                nxt = op.apply_schema(schema)
                doc = op.apply_instance(schema, doc, ctx)
                errs = errors(relaxed(nxt, {"#": version_fields}), doc)
                if errs:
                    raise O.MigrationError(
                        f"edge {edge.name}, op #{i + 1} ({op.describe()}): document is not valid "
                        f"afterwards:\n  " + "\n  ".join(map(str, errs)))
                schema = nxt
        if f_to:
            doc = O.insert_at(doc, f_to, edge.to, pos if (pos is not None and f_to == f_from) else 0)
        post = errors(schemas[edge.to], doc)
        if post:
            raise O.MigrationError(f"migrated document is not valid under {edge.to}:\n  "
                                   + "\n  ".join(map(str, post)))
        er.changes = ctx.changes
        cur = edge.to
    return doc, report

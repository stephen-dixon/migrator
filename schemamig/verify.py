"""Fixture-driven verification (what CI would run).

fixtures/<kind>/<version>/<name>.json:
  * every fixture must validate under its version's schema
  * a fixture with the same name at a later version is a golden expected migration output
  * backward-compatible edges: every fixture at `from` must validate UNCHANGED under `to`
    (only the version field restamped) -- this is what makes the reviewer's claim falsifiable
"""
from __future__ import annotations

from pathlib import Path

from . import ops as O
from .migrate import migrate
from .project import Kind, dump_json, load_json, vkey
from .schema import errors


def verify(kind: Kind) -> list[str]:
    problems: list[str] = []
    if not kind.fixtures or not kind.fixtures.is_dir():
        return [f"{kind.name}: no fixtures directory configured"]
    schemas = kind.schemas()
    by_version: dict[str, dict[str, Path]] = {}
    for vdir in sorted(kind.fixtures.iterdir()):
        if vdir.is_dir():
            if vdir.name not in schemas:
                problems.append(f"{vdir}: unknown version directory")
                continue
            by_version[vdir.name] = {p.name: p for p in sorted(vdir.glob("*.json"))}
    unversioned = kind.version_field(kind.baseline_version) is None

    for v, files in by_version.items():
        for name, p in files.items():
            errs = errors(schemas[v], load_json(p))
            if errs:
                problems.append(f"{p}: invalid under {v}: {errs[0]}")

    for e in kind.chain():
        if e.compatibility != "backward-compatible":
            continue
        f_from, f_to = kind.version_field(e.from_), kind.version_field(e.to)
        for name, p in by_version.get(e.from_, {}).items():
            doc = load_json(p)
            if isinstance(doc, dict):
                doc = {k: v for k, v in doc.items() if k != f_from}
                if f_to:
                    doc = {f_to: e.to, **doc}
            errs = errors(schemas[e.to], doc)
            if errs:
                problems.append(f"{e.file.name}: claimed backward-compatible, but fixture {p.name} "
                                f"({e.from_}) is invalid under {e.to} unchanged: {errs[0]}")

    versions = sorted(by_version, key=vkey)
    for i, v in enumerate(versions):
        for name, p in by_version[v].items():
            for w in versions[i + 1:]:
                if name not in by_version[w]:
                    continue
                fv = v if (unversioned and v == kind.baseline_version) else None
                try:
                    out, _ = migrate(kind, load_json(p), to=w, from_version=fv)
                except O.MigrationError as err:
                    problems.append(f"{p} -> {w}: migration failed: {err}")
                    continue
                expected = by_version[w][name].read_text(encoding="utf-8")
                if dump_json(out) != expected:
                    problems.append(f"{p} -> {w}: output differs from golden {by_version[w][name]}")
    return problems

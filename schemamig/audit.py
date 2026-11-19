"""`change show <id>`: the audit view of one recorded change.

Answers "does this record reflect what I meant?" three ways:
  1. what was declared vs. what the tool inferred, and a sentence per operation
  2. the schema diff the change accounts for
  3. its effect on real fixture documents (before/after)
Works for unreleased (journal) and released (release record) changes.
"""
from __future__ import annotations

import copy
import difflib
from typing import Any

from . import journal as J
from . import ops as O
from .project import Kind, Project, dump_json, load_json

# option name (declared) -> op keys it determines
_DECLARES = {"values": {"values"}, "from": {"from"}, "to": {"to"}, "name": {"name"}, "default": {"default"},
             "lossy": {"lossy"}, "managed_version": {"managed", "required"}, "convert": {"convert"},
             "fields": {"fields"}, "into": {"into"}, "adapter": {"module", "function"}}


def _diff(a: str, b: str, name_a: str, name_b: str, limit: int) -> list[str]:
    # autojunk off: JSON has many identical lines ("},", "type": "string") that the default
    # heuristic would treat as junk, producing misaligned, noisy hunks
    al, bl = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    lines = [f"--- {name_a}", f"+++ {name_b}"] if sm.ratio() < 1 else []
    for group in sm.get_grouped_opcodes(2):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        lines.append(f"@@ -{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1} @@")
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                lines += [" " + x for x in al[a1:a2]]
            else:
                lines += ["-" + x for x in al[a1:a2]] + ["+" + x for x in bl[b1:b2]]
    return lines[:limit] + ([f"... ({len(lines) - limit} more lines)"] if len(lines) > limit else [])


def _locate(kind: Kind, cid: str) -> dict:
    eff = J.effective(J.read_records(kind))
    ids = [e["id"] for e in eff]
    if cid in ids:
        i = ids.index(cid)
        return {"status": "unreleased", "entry": eff[i], "from_version": J.head(kind),
                "base": kind.schemas()[J.head(kind)],
                "before_ops": [o for e in eff[:i] for o in e["ops"]]}
    for edge in kind.chain():
        pos = 0
        for c in edge.changes:
            n = c.get("operations")
            if c.get("id") == cid:
                if n is None:
                    raise J.JournalError(f"{cid} was released without per-change operation counts")
                spec = [o.spec for o in edge.operations]
                return {"status": f"released in {edge.to}", "entry": {**c, "ops": spec[pos:pos + n]},
                        "from_version": edge.from_, "base": kind.schemas()[edge.from_],
                        "before_ops": spec[:pos], "base_dir": edge.file.parent}
            pos += n or 0
    raise J.JournalError(f"no change {cid!r} in the journal or in any release record")


def show(project: Project, kind: Kind, cid: str, max_lines: int = 60) -> dict:
    loc = _locate(kind, cid)
    entry, base_dir = loc["entry"], loc.get("base_dir", kind.root)
    parse = lambda specs: [O.parse_op(copy.deepcopy(s), base_dir) for s in specs]  # noqa: E731
    before, mine = parse(loc["before_ops"]), parse(entry["ops"])

    s = loc["base"]
    for op in before:
        s = op.apply_schema(s)
    pre = s
    for op in mine:
        s = op.apply_schema(s)
    post = s

    declared = entry.get("options") or {}
    covered = set().union(*(_DECLARES.get(k, {k}) for k in declared)) if declared else set()
    inferred = {}
    for o in entry["ops"]:
        for k, v in o.items():
            if k in ("op", "at") or k in covered:
                continue
            inferred[f"{o['op']}.{k}"] = "(taken from your schema edit)" if k in ("schema", "patch", "schema_patch") else v

    fixtures = []
    fdir = kind.fixtures / loc["from_version"] if kind.fixtures else None
    vf = kind.version_field(loc["from_version"])
    for p in sorted(fdir.glob("*.json")) if fdir and fdir.is_dir() else []:
        doc = load_json(p)
        if vf and isinstance(doc, dict):
            doc = {k: v for k, v in doc.items() if k != vf}
        sch = loc["base"]
        ctx = O.Ctx(loc["from_version"], "show", base_dir)
        try:
            for op in before:
                nxt = op.apply_schema(sch)
                doc = op.apply_instance(sch, doc, ctx)
                sch = nxt
            ctx.changes.clear()
            after = doc
            for op in mine:
                nxt = op.apply_schema(sch)
                after = op.apply_instance(sch, after, ctx)
                sch = nxt
        except O.MigrationError as e:
            fixtures.append({"fixture": p.name, "error": str(e)})
            continue
        fixtures.append({"fixture": p.name, "changes": [str(c) for c in ctx.changes],
                         "diff": _diff(dump_json(doc), dump_json(after), p.name, p.name + " (after)", max_lines)})

    return {
        "id": cid, "status": loc["status"], "class": entry["class"], "target": entry["target"],
        "reason": entry.get("reason", ""), "compatibility": entry.get("compatibility", "unknown"),
        "declared": declared, "inferred": inferred,
        "summary": [op.describe() for op in mine],
        "effects": [op.instance_effect() for op in mine],
        "schema_diff": _diff(dump_json(pre), dump_json(post), "schema (before)", "schema (after)", max_lines),
        "fixtures": fixtures,
    }


def render(r: dict) -> str:
    out = [f"{r['id']}  {r['class']} {r['target']}   [{r['status']}; compatibility: {r['compatibility']}]"]
    if r["reason"]:
        out.append(f"  reason: {r['reason']}")
    out.append("  does:")
    out += [f"    - {s}   [{e}]" for s, e in zip(r["summary"], r["effects"])]
    out.append("  you declared: " + (", ".join(f"{k}={v!r}" for k, v in r["declared"].items()) or "(nothing; all inferred)"))
    if r["inferred"]:
        out.append("  tool inferred: " + ", ".join(f"{k}={v!r}" if not str(v).startswith("(") else f"{k} {v}"
                                                 for k, v in r["inferred"].items()))
    out.append("\n  schema change:")
    out += ["    " + line for line in r["schema_diff"]] or ["    (none)"]
    for f in r["fixtures"]:
        out.append(f"\n  effect on fixture {f['fixture']}:")
        if "error" in f:
            out.append(f"    ERROR: {f['error']}")
        elif not f["changes"]:
            out.append("    (unchanged)")
        else:
            out += [f"    * {c}" for c in f["changes"]] + ["    " + line for line in f["diff"]]
    if not r["fixtures"]:
        out.append("\n  (no fixtures at this version to show document effects)")
    return "\n".join(out)

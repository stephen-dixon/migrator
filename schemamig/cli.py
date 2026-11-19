"""schemamig: operation-sourced JSON Schema evolution (proof of concept)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import impact as I
from . import ops as O
from .migrate import detect_version, migrate
from .project import Project, dump_json, generate, load_json
from .schema import SchemaError, check_supported, errors


def _project(args) -> Project:
    return Project(Path(args.project)) if args.project else Project.find(Path.cwd())


def cmd_build(args) -> int:
    kinds = [_project(args).kind(args.kind)] if args.kind else _project(args).kinds.values()
    drift = False
    for k in kinds:
        for path, status in generate(k, write=not args.check):
            print(f"{status:>8}  {path.relative_to(k.root)}")
            drift |= status != "ok"
        for w in k.warnings:
            print(f"warning: {w}")
    if args.check and drift:
        print("generated schemas are out of date with the changesets; run `schemamig build`")
        return 2
    return 0


def cmd_explain(args) -> int:
    k = _project(args).kind(args.kind)
    k.schemas()
    print(f"{k.name}: baseline {k.baseline_version} ({k.baseline.relative_to(k.root)})")
    for e in k.chain():
        if args.edge and e.from_ != args.edge:
            continue
        a = e.assessment()
        print(f"\n{e.from_} -> {e.to}  ({e.file.relative_to(k.root)})")
        if e.description:
            print(f"  {e.description.strip()}")
        print(f"  implementation:     {a['implementation']}")
        print(f"  instance migration: {a['instance_migration']}")
        print(f"  compatibility:      {a['compatibility_claim']} (reviewer claim)")
        vf_old, vf_new = k.version_field(e.from_), k.version_field(e.to)
        if vf_old != vf_new:
            print(f"  version field:      {vf_old!r} -> {vf_new!r}")
        if e.kind == "manual-baseline":
            print(f"  schema:             {e.schema_file.relative_to(k.root)}")
            print(f"  reason:             {e.reason}")
            if e.adapter:
                print(f"  adapter:            {e.adapter.describe()}")
        for i, op in enumerate(e.operations):
            print(f"  {i + 1:>2}. [{op.instance_effect():<14}] {op.describe()}")
            for n in op.notes:
                print(f"      note: {n}")
    for w in k.warnings:
        print(f"warning: {w}")
    return 0


def cmd_migrate(args) -> int:
    k = _project(args).kind(args.kind)
    src, out = Path(args.document), Path(args.out) if args.out else None
    doc = load_json(src)
    new, rep = migrate(k, doc, to=args.to, from_version=args.from_version)
    print(rep.render())
    if rep.noop:
        return 0
    if args.dry_run:
        print("(dry run: nothing written)")
        return 0
    if out is None:
        raise O.MigrationError("pass --out FILE (or --dry-run); migration never writes in place")
    if out.resolve() == src.resolve():
        raise O.MigrationError("refusing to migrate in place; choose a different --out")
    if out.exists() and not args.force:
        raise O.MigrationError(f"{out} exists; pass --force to overwrite")
    original = src.read_text(encoding="utf-8")
    if dump_json(doc) != original:
        print("note: input was not in canonical formatting (2-space JSON); the output also reformats it")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_json(new), encoding="utf-8")
    print(f"wrote {out}")
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(rep.to_json(), indent=2) + "\n")
    return 0


def cmd_validate(args) -> int:
    k = _project(args).kind(args.kind)
    doc = load_json(Path(args.document))
    v = args.version or detect_version(k, doc, None)
    errs = errors(k.schemas()[v], doc)
    if errs:
        print(f"INVALID under {k.name} {v}:")
        for e in errs:
            print(f"  {e}")
        return 1
    print(f"valid under {k.name} {v}")
    return 0


def cmd_impact(args) -> int:
    k = _project(args).kind(args.kind)
    edge = k.edge(args.edge)
    docs = [(d, load_json(Path(d))) for d in args.documents]
    rep = I.impact(k, edge, docs, from_version=args.from_version)
    print(json.dumps(rep, indent=2) if args.json else I.render(rep))
    return 0


def cmd_verify(args) -> int:
    from .verify import verify
    proj = _project(args)
    kinds = [proj.kind(args.kind)] if args.kind else list(proj.kinds.values())
    bad = 0
    for k in kinds:
        problems = verify(k)
        print(f"{k.name}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
        for pr in problems:
            print(f"  {pr}")
        bad += len(problems)
    return 1 if bad else 0


def cmd_record_schema(args) -> int:
    from .records import SCHEMA_PATH, build_schema
    text = dump_json(build_schema())
    current = SCHEMA_PATH.read_text() if SCHEMA_PATH.exists() else None
    if args.check:
        print("record schema up to date" if current == text else "record schema stale; run `schemamig record-schema`")
        return 0 if current == text else 2
    SCHEMA_PATH.write_text(text)
    print(f"wrote {SCHEMA_PATH}")
    return 0


def cmd_lint(args) -> int:
    schema = load_json(Path(args.schema))
    for w in check_supported(schema):
        print(f"warning: {w}")
    print("supported")
    return 0


def cmd_new(args) -> int:
    k = _project(args).kind(args.kind)
    head = k.versions()[-1]
    n = len(k.chain()) + 1
    path = k.migrations / f"{n:04d}-{head}-to-{args.to}.yaml"
    if path.exists():
        raise O.DslError(f"{path} exists")
    body = {"from": head, "to": args.to, "document_kind": k.name, "description": "TODO",
            "compatibility": "unknown"}
    if args.manual:
        body.update(kind="manual-baseline", schema=f"../../baselines/{k.name}-{args.to}.json",
                    reason="TODO", instance_migration="unsupported")
    else:
        body["operations"] = []
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    print(f"created {path.relative_to(k.root)}")
    return 0


# --- journal workflow ----------------------------------------------------------------

def _opts(args) -> dict:
    o = {}
    if getattr(args, "default", None) is not None:
        try:
            o["default"] = json.loads(args.default)
        except json.JSONDecodeError:
            o["default"] = args.default
    for k in ("name", "convert", "adapter", "into"):
        if getattr(args, k, None):
            o[k] = getattr(args, k)
    if getattr(args, "from_", None):
        o["from"] = args.from_
    if getattr(args, "to", None):
        o["to"] = args.to
    if getattr(args, "fields", None):
        o["fields"] = [f.strip() for f in args.fields.split(",") if f.strip()]
    if getattr(args, "map", None):
        def val(x):
            try:
                return json.loads(x)
            except json.JSONDecodeError:
                return x
        vals = {}
        for m in args.map:
            if "=" not in m:
                raise O.DslError(f"--map expects OLD=NEW, got {m!r}")
            a, b = m.split("=", 1)
            vals[val(a)] = val(b)
        o["values"] = vals
    if getattr(args, "lossy", False):
        o["lossy"] = True
    if getattr(args, "managed_version", False):
        o["managed_version"] = True
    return o


def _out(args, data, text: str) -> None:
    print(json.dumps(data, indent=2) if getattr(args, "json", False) else text)


def cmd_change(args) -> int:
    from . import journal as J
    a = args.action
    if a == "classes":
        for c, d in J.CLASSES.items():
            print(f"{c:<17} {d}")
        return 0
    proj = _project(args)
    if a in ("begin", "record"):
        kind = proj.kind(args.kind)
        s = J.begin(proj, kind, args.cls, args.target, _opts(args), args.reason or "", args.compat)
        if a == "begin":
            _out(args, s, f"opened {s['class']} {s['target']} on {kind.name}; edit the schema, then "
                          "`schemamig change commit`")
            return 0
        try:
            rec = J.commit(proj)
        except Exception:
            J.abort(proj)
            raise
        _out(args, rec, f"recorded {rec['id']}: {rec['class']} {rec['target']} ({len(rec['ops'])} op(s))")
        return 0
    if a == "status":
        st = J.status(proj)
        if not st["open"]:
            _out(args, st, "no change open")
        elif st["ready"]:
            ops = "\n".join(f"  {json.dumps(o)}" for o in st["ops"])
            _out(args, st, f"open: {st['class']} {st['target']}" + (f" (amending {st['amends']})" if st.get("amends") else "")
                 + f"\nready to commit; inferred:\n{ops}")
        else:
            _out(args, st, f"open: {st['class']} {st['target']}\nnot ready: {st['problem']}")
        return 0
    if a == "commit":
        rec = J.commit(proj, args.reason, args.compat)
        _out(args, rec, f"recorded {rec['id']} ({rec['type']}): {rec['class']} {rec['target']}\n"
             + "\n".join(f"  {json.dumps(o)}" for o in rec["ops"]))
        return 0
    if a == "abort":
        s = J.abort(proj)
        _out(args, s, "aborted (your edits to the schema are untouched)" if s else "no change open")
        return 0
    if a == "reopen":
        s = J.reopen(proj, proj.kind(args.kind), args.id)
        _out(args, s, f"reopened {args.id}: {s['class']} {s['target']}; edit, then `schemamig change commit`")
        return 0
    if a == "drop":
        rec = J.drop(proj.kind(args.kind), args.id)
        _out(args, rec, f"dropped {args.id} (revert its edit in the schema, or `check` will fail)")
        return 0
    if a == "show":
        from . import audit
        r = audit.show(proj, proj.kind(args.kind), args.id)
        _out(args, r, audit.render(r))
        return 0
    if a == "list":
        eff = J.effective(J.read_records(proj.kind(args.kind)))
        lines = [f"{e['id']}  {e['class']:<16} {e['target']:<28} [{e.get('compatibility') or 'unknown'}] "
                 f"{e.get('reason', '')}" + ("  (amended)" if e.get("amended_by") else "") for e in eff]
        _out(args, eff, "\n".join(lines) or "journal is empty")
        return 0
    raise AssertionError(a)


def cmd_gate(args) -> int:
    from . import journal as J
    from .verify import verify
    proj = _project(args)
    kinds = [proj.kind(args.kind)] if args.kind else list(proj.kinds.values())
    report, bad = {}, 0
    for k in kinds:
        errs, warns = (J.check(proj, k) if k.schema_file else ([], []))
        errs += [f"generated schema stale: {p.name}" for p, st in generate(k, write=False) if st != "ok"]
        errs += verify(k) if k.fixtures else []
        report[k.name] = {"errors": errs, "warnings": warns}
        bad += len(errs)
    lines = []
    for name, r in report.items():
        lines.append(f"{name}: {'OK' if not r['errors'] else 'FAIL'}")
        lines += [f"  error: {e}" for e in r["errors"]] + [f"  note: {w}" for w in r["warnings"]]
    _out(args, report, "\n".join(lines))
    return 1 if bad else 0


def cmd_release(args) -> int:
    from . import journal as J
    proj = _project(args)
    k = proj.kind(args.kind)
    rec = J.release(proj, k, args.version, args.compat, args.description or "")
    print(f"released {k.name} {args.version}: {rec.relative_to(k.root)}")
    return 0


def add_journal_commands(sub) -> None:
    compat = ["backward-compatible", "breaking", "unknown"]
    p = sub.add_parser("change", help="granular change scopes recorded in the journal")
    ch = p.add_subparsers(dest="action", required=True)
    for name, help_ in (("begin", "open a change scope"), ("record", "begin + commit in one step (after editing)")):
        q = ch.add_parser(name, help=help_)
        q.add_argument("cls", metavar="class")
        q.add_argument("target", help="def:<name>, #/pointer or $.path (anything for unclassified)")
        q.add_argument("--kind")
        q.add_argument("--reason")
        q.add_argument("--compat", choices=compat)
        q.add_argument("--name", help="pick the property/field when ambiguous")
        q.add_argument("--default", help="JSON value for documents lacking a now-required field")
        q.add_argument("--lossy", action="store_true")
        q.add_argument("--managed-version", action="store_true")
        q.add_argument("--convert", choices=sorted(O.CONVERTERS))
        q.add_argument("--adapter", help="module.py:function (unclassified only), path relative to project")
        q.add_argument("--from", dest="from_", help="rename-field: old property name")
        q.add_argument("--to", help="rename-field: new property name")
        q.add_argument("--map", action="append", metavar="OLD=NEW", help="rename-value: one per renamed value")
        q.add_argument("--fields", help="nest-fields: comma-separated properties to move")
        q.add_argument("--into", help="nest-fields / wrap: name of the new property")
        q.add_argument("--json", action="store_true")
    q = ch.add_parser("commit")
    q.add_argument("--reason")
    q.add_argument("--compat", choices=compat)
    q.add_argument("--json", action="store_true")
    for name in ("status", "abort", "classes"):
        ch.add_parser(name).add_argument("--json", action="store_true")
    for name in ("reopen", "drop", "show"):
        q = ch.add_parser(name)
        q.add_argument("id")
        q.add_argument("--kind")
        q.add_argument("--json", action="store_true")
    q = ch.add_parser("list")
    q.add_argument("--kind")
    q.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_change)

    p = sub.add_parser("check", help="CI gate: journal explains the working schema; generated/fixtures OK")
    p.add_argument("--kind")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("release", help="fold the journal into a release record + schema snapshot")
    p.add_argument("version")
    p.add_argument("--kind")
    p.add_argument("--compat", choices=compat)
    p.add_argument("--description")
    p.set_defaults(fn=cmd_release)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="schemamig", description=__doc__)
    ap.add_argument("--project", help="path to schemamig.yaml (default: search upwards from cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="generate schemas for every version from baseline + changesets")
    p.add_argument("--kind")
    p.add_argument("--check", action="store_true", help="fail if committed generated schemas are stale")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("explain", help="show each edge's operations and classification")
    p.add_argument("--kind")
    p.add_argument("--edge", help="only the edge leaving this version")
    p.set_defaults(fn=cmd_explain)

    p = sub.add_parser("migrate", help="migrate a document to a new file")
    p.add_argument("document")
    p.add_argument("--kind")
    p.add_argument("--to")
    p.add_argument("--from-version", help="source version for legacy/unversioned documents")
    p.add_argument("--out")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report-json")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("validate", help="validate a document against its (or a given) version")
    p.add_argument("document")
    p.add_argument("--kind")
    p.add_argument("--version")
    p.set_defaults(fn=cmd_validate)

    add_journal_commands(sub)

    p = sub.add_parser("impact", help="non-destructive impact report for a manual-baseline edge")
    p.add_argument("documents", nargs="+")
    p.add_argument("--kind")
    p.add_argument("--edge", required=True, help="the 'from' version of the manual-baseline edge")
    p.add_argument("--from-version")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_impact)

    p = sub.add_parser("verify", help="validate fixtures, check compat claims, compare golden migrations")
    p.add_argument("--kind")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("record-schema", help="write (or --check) the generated record-format JSON Schema")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_record_schema)

    p = sub.add_parser("lint-schema", help="check a schema uses only the supported subset")
    p.add_argument("schema")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("new", help="create a changeset skeleton at the head of the chain")
    p.add_argument("--kind")
    p.add_argument("--to", required=True)
    p.add_argument("--manual", action="store_true", help="manual-baseline edge")
    p.set_defaults(fn=cmd_new)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (O.DslError, O.MigrationError, SchemaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

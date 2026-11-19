"""Journal workflow: granular, intent-annotated change scopes over a hand-edited schema.

  change begin <class> <target>  -> declare intent (session in .schemamig/session.json)
  ... edit the working schema in your editor ...
  change commit                  -> infer operations from the scoped diff, verify by replay,
                                    append one JSON line to the journal
  change reopen <id>             -> amend a previous change (appends an 'amend' record)
  check                          -> CI gate: journal replays to the working schema
  release <version>              -> fold the journal into a release record + schema snapshot

The session is a convenience only. The *expected* schema is always
replay(last release, journal), so a lost session or edits made before `begin` cannot
corrupt anything: commit and check compare against the replay, not a stored pre-image.

Journal: JSON Lines, append-only, `merge=union` in .gitattributes. Each record has an
`id` and the `after` id it was written on top of, so a union merge of two branches is
detectable (two records with the same `after`) and checked for overlapping locations.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import yaml

from . import ops as O
from . import pointer as P
from . import schemadiff as D
from .project import Kind, Project, dump_json, load_json, vkey
from .records import RECORD_VERSION
from .schema import SchemaError, check_supported, deref, errors, relaxed, resolve_location, schema_id

CLASSES = {
    "add-field": "add one property to an object (options: required via the schema edit, --default, "
                 "--managed-version, --name)",
    "rename-field": "rename one property (--from/--to, or inferred)",
    "remove-field": "remove one property (--lossy to approve deleting values)",
    "make-required": "add one name to 'required' (--default)",
    "make-optional": "remove one name from 'required'",
    "set-field-schema": "change one property's schema (--convert to-number|to-string|to-array)",
    "rename-value": "rename enum/const values (--name <field>; --map OLD=NEW per value, required "
                    "unless exactly one value changed)",
    "nest-fields": "move properties into a new sub-object (--fields a,b --into x, or inferred)",
    "wrap": "make the target the value of a new single property (--into, or inferred)",
    "add-variant": "add union branch(es), plus any new $defs they reference",
    "unclassified": "anything else: recorded as a raw schema patch (--adapter module:function to "
                    "migrate documents)",
}
COMPAT_ORDER = ["backward-compatible", "unknown", "breaking"]


class JournalError(O.DslError):
    pass


# --- storage -----------------------------------------------------------------

def _need(kind: Kind) -> None:
    if not (kind.schema_file and kind.journal):
        raise JournalError(f"kind {kind.name!r} has no 'schema' (working file) and 'journal' configured")


def read_records(kind: Kind) -> list[dict]:
    _need(kind)
    if not kind.journal.exists():
        return []
    recs, seen = [], set()
    for n, line in enumerate(kind.journal.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            raise JournalError(f"{kind.journal.name}:{n}: not valid JSON ({e}); a merge may have split a line")
        from .records import validate
        bad = validate("journal_record", r)
        if bad:
            raise JournalError(f"{kind.journal.name}:{n}: invalid record: " + "; ".join(bad))
        if r.get("id") in seen:
            raise JournalError(f"{kind.journal.name}:{n}: duplicate id {r.get('id')!r}")
        seen.add(r.get("id"))
        recs.append(r)
    return recs


def append_record(kind: Kind, rec: dict) -> None:
    kind.journal.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    assert "\n" not in line
    with open(kind.journal, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def new_id() -> str:
    return "c" + secrets.token_hex(4)


def effective(records: list[dict]) -> list[dict]:
    """Apply amend/drop records; returns change entries in journal order."""
    out: list[dict] = []
    index: dict[str, int] = {}
    for r in records:
        t = r.get("type")
        if t == "change":
            index[r["id"]] = len(out)
            out.append(r)
        elif t == "amend":
            if r["amends"] not in index:
                raise JournalError(f"record {r['id']} amends unknown change {r['amends']}")
            out[index[r["amends"]]] = {**r, "id": r["amends"], "amended_by": r["id"]}
        elif t == "drop":
            if r["drops"] not in index:
                raise JournalError(f"record {r['id']} drops unknown change {r['drops']}")
            out[index[r["drops"]]] = None
        else:
            raise JournalError(f"record {r.get('id')}: unknown type {t!r}")
    return [e for e in out if e is not None]


# --- replay -----------------------------------------------------------------

def head(kind: Kind) -> str:
    return kind.versions()[-1]


def parse_entry_ops(kind: Kind, entry: dict) -> list[O.Operation]:
    return [O.parse_op(copy.deepcopy(o), kind.root) for o in entry["ops"]]


def replay(kind: Kind, entries: list[dict]) -> tuple[dict, dict[str, set[str]]]:
    s = kind.schemas()[head(kind)]
    touched: dict[str, set[str]] = {}
    for e in entries:
        before = s
        try:
            for op in parse_entry_ops(kind, e):
                s = op.apply_schema(s)
                check_supported(s)
        except (O.DslError, SchemaError) as err:
            raise JournalError(f"change {e['id']} ({e['class']} {e['target']}) no longer applies: {err}") from None
        touched[e["id"]] = D.touched(before, s)
    return s, touched


def working(kind: Kind) -> dict:
    _need(kind)
    return load_json(kind.schema_file)


# --- inference -----------------------------------------------------------------

def _loc(pre: dict, post: dict, target: str) -> tuple[str, Any, Any]:
    try:
        ptr = resolve_location(pre, target)
    except SchemaError as e:
        raise JournalError(f"target {target!r}: {e}") from None
    try:
        post_node = P.get(post, ptr)
    except (KeyError, IndexError):
        raise JournalError(f"target {target!r} ({ptr}) no longer exists in the working schema") from None
    return ptr, P.get(pre, ptr), post_node


def _one(items: list, what: str, name: str | None = None) -> str:
    if name is not None:
        if name not in items:
            raise JournalError(f"{what}: {name!r} not among {items}")
        return name
    if len(items) != 1:
        raise JournalError(f"{what}: expected exactly one, found {items or 'none'} "
                           "(one change per scope; use --name to pick)")
    return items[0]


def _props(node: Any) -> dict:
    return node.get("properties", {}) if isinstance(node, dict) else {}


def _strip_annotation_ops(patch: list[dict]) -> list[dict]:
    out = []
    for op in patch:
        toks = P.split("#" + op["path"])
        if toks and toks[-1] in D.ANNOTATIONS and not (len(toks) > 1 and toks[-2] in D.NAME_MAPS):
            continue
        out.append(op)
    return out


def infer(cls: str, target: str, opts: dict, reason: str, pre: dict, post: dict) -> list[dict]:
    if cls not in CLASSES:
        raise JournalError(f"unknown change class {cls!r}; choose from {sorted(CLASSES)}")
    name = opts.get("name")
    if cls == "unclassified":
        if not D.residual(pre, post):
            raise JournalError("no validation-relevant change found (annotation edits need no record)")
        if not reason:
            raise JournalError("unclassified changes need --reason")
        patch = _strip_annotation_ops(D.patch(pre, post))
        if opts.get("adapter"):
            mod, _, fn = opts["adapter"].partition(":")
            return [{"op": "adapter", "module": mod, "function": fn or "migrate", "reason": reason,
                     "schema_patch": patch}]
        return [{"op": "patch-schema", "reason": reason, "patch": patch}]

    ptr, a, b = _loc(pre, post, target)
    pa, pb = _props(a), _props(b)
    added = [k for k in pb if k not in pa]
    gone = [k for k in pa if k not in pb]

    if cls == "add-field":
        k = _one(added, "new property", name)
        spec: dict = {"op": "add-field", "at": target, "name": k, "schema": copy.deepcopy(pb[k])}
        if k in (b.get("required") or []):
            spec["required"] = True
            if opts.get("managed_version"):
                spec["managed"] = "version"
            elif "default" in opts:
                spec["default"] = opts["default"]
            else:
                raise JournalError(f"{k!r} is required but has no --default; existing documents lack it. "
                                   "Give --default, make it optional, or use an unclassified change "
                                   "with an --adapter")
        elif opts.get("managed_version"):
            raise JournalError("--managed-version requires the field to be in 'required'")
        if list(pb).index(k) == 0 and len(pb) > 1:
            spec["position"] = "start"
        return [spec]
    if cls == "rename-field":
        return [{"op": "rename-field", "at": target,
                 "from": opts.get("from") or _one(gone, "removed property"),
                 "to": opts.get("to") or _one(added, "added property")}]
    if cls == "remove-field":
        k = _one(gone, "removed property", name)
        return [{"op": "remove-field", "at": target, "name": k, **({"lossy": True} if opts.get("lossy") else {})}]
    if cls in ("make-required", "make-optional"):
        ra, rb = set(a.get("required", [])), set(b.get("required", []))
        diff = sorted(rb - ra) if cls == "make-required" else sorted(ra - rb)
        spec = {"op": cls, "at": target, "name": _one(diff, f"{cls} name", name)}
        if cls == "make-required" and "default" in opts:
            spec["default"] = opts["default"]
        return [spec]
    if cls == "set-field-schema":
        changed = [k for k in pa if k in pb and not D.equivalent(pa[k], pb[k])]
        k = _one(changed, "property with changed schema", name)
        spec = {"op": "set-field-schema", "at": target, "name": k, "schema": copy.deepcopy(pb[k])}
        if opts.get("convert"):
            spec["convert"] = opts["convert"]
        return [spec]
    if cls == "rename-value":
        vptr = ptr
        if name:
            if name not in pa:
                raise JournalError(f"no property {name!r} at {ptr}")
            vptr = deref(pre, P.child(ptr, "properties", name))
        va, vb = P.get(pre, vptr), P.get(post, vptr)
        if opts.get("values"):
            mapping = dict(opts["values"])  # explicit: replay verifies the edit matches
        elif "const" in va and "const" in vb:
            mapping = {va["const"]: vb["const"]} if va["const"] != vb["const"] else {}
        elif "enum" in va and "enum" in vb:
            old = [x for x in va["enum"] if x not in vb["enum"]]
            new = [x for x in vb["enum"] if x not in va["enum"]]
            if len(old) == 1 and len(new) == 1:
                mapping = {old[0]: new[0]}
            elif old or new:
                raise JournalError(f"rename-value at {vptr}: {old} removed and {new} added; say which "
                                   "becomes which with --map OLD=NEW (one per value)")
            else:
                mapping = {}
        else:
            raise JournalError(f"rename-value: no enum/const at {vptr}")
        if not mapping:
            raise JournalError(f"rename-value: no value changed at {vptr}")
        return [{"op": "rename-value", "at": target, **({"name": name} if name else {}), "values": mapping}]
    if cls == "nest-fields":
        into = opts.get("into") or _one(added, "new container property")
        fields = opts.get("fields") or [k for k in pa if k in gone]
        if not fields:
            raise JournalError("nest-fields: no properties were moved")
        return [{"op": "nest-fields", "at": target, "fields": fields, "into": into,
                 "schema": copy.deepcopy(pb[into])}]
    if cls == "wrap":
        inner = {k: v for k, v in a.items() if k not in O.HOIST} if ptr == "#" else a
        cands = [k for k, v in pb.items() if D.equivalent(v, inner)]
        into = opts.get("into") or _one(cands, "property holding the wrapped schema", name)
        return [{"op": "wrap", "at": target, "into": into}]
    if cls == "add-variant":
        kw = "oneOf" if "oneOf" in a else "anyOf" if "anyOf" in a else None
        if kw is None:
            raise JournalError(f"add-variant: {ptr} has no oneOf/anyOf")
        known = {json.dumps(D.normalize(x), sort_keys=True) for x in a[kw]}
        new_branches = [x for x in b.get(kw, []) if json.dumps(D.normalize(x), sort_keys=True) not in known]
        if not new_branches:
            raise JournalError("add-variant: no new union branch found")
        new_defs = [d for d in post.get("$defs", {}) if d not in pre.get("$defs", {})]
        ops, used = [], set()
        for br in new_branches:
            ref = br.get("$ref", "") if isinstance(br, dict) else ""
            d = ref[len("#/$defs/"):] if ref.startswith("#/$defs/") else None
            if d in new_defs:
                ops.append({"op": "add-def", "name": d, "schema": copy.deepcopy(post["$defs"][d])})
                used.add(d)
                ops.append({"op": "add-variant", "at": target, "ref": ref})
            else:
                ops.append({"op": "add-variant", "at": target, "schema": copy.deepcopy(br)})
        if set(new_defs) - used:
            raise JournalError(f"add-variant: new $defs {sorted(set(new_defs) - used)} are not used by the new "
                               "branches; record them separately")
        return ops
    raise AssertionError(cls)


# --- sessions -------------------------------------------------------------------

def session_path(project: Project) -> Path:
    return project.root / ".schemamig" / "session.json"


def load_session(project: Project) -> dict | None:
    p = session_path(project)
    return json.loads(p.read_text()) if p.exists() else None


def _save_session(project: Project, s: dict | None) -> None:
    p = session_path(project)
    if s is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")


def begin(project: Project, kind: Kind, cls: str, target: str, opts: dict, reason: str = "",
          compatibility: str | None = None, amends: str | None = None) -> dict:
    if load_session(project):
        raise JournalError("a change is already open; commit or abort it first")
    if cls not in CLASSES:
        raise JournalError(f"unknown change class {cls!r}; choose from {sorted(CLASSES)}")
    eff = effective(read_records(kind))
    expected, _ = replay(kind, [e for e in eff if e["id"] != amends])
    if cls != "unclassified":
        resolve_location(expected, target)  # fail early on a bad target
    s = {"kind": kind.name, "class": cls, "target": target, "options": opts, "reason": reason,
         "compatibility": compatibility, "amends": amends}
    _save_session(project, s)
    return s


def _plan(kind: Kind, s: dict) -> tuple[list[dict], dict]:
    """Infer the ops for session s and verify a full replay matches the working schema."""
    eff = effective(read_records(kind))
    amends = s.get("amends")
    others = [e for e in eff if e["id"] != amends]
    pre, _ = replay(kind, others)
    post = working(kind)
    ops = infer(s["class"], s["target"], s.get("options") or {}, s.get("reason") or "", pre, post)
    for o in ops:
        O.parse_op(copy.deepcopy(o), kind.root)
    entry = {"id": amends or "pending", "class": s["class"], "target": s["target"], "ops": ops}
    candidate = [entry if e["id"] == amends else e for e in eff] if amends else eff + [entry]
    final, _ = replay(kind, candidate)
    res = D.residual(final, post)
    if res:
        raise JournalError("the working schema has changes this scope does not explain "
                           "(record them as separate changes, or revert them):\n  " + "\n  ".join(res))
    return ops, entry


def status(project: Project) -> dict:
    s = load_session(project)
    if not s:
        return {"open": False}
    kind = project.kind(s["kind"])
    try:
        ops, _ = _plan(kind, s)
        return {"open": True, **s, "ready": True, "ops": ops}
    except (O.DslError, SchemaError) as e:
        try:
            expected, _ = replay(kind, [x for x in effective(read_records(kind)) if x["id"] != s.get("amends")])
            if not D.residual(expected, working(kind)):
                return {"open": True, **s, "ready": False, "problem": "waiting for your edit"}
        except (O.DslError, SchemaError):
            pass
        return {"open": True, **s, "ready": False, "problem": str(e)}


def commit(project: Project, reason: str | None = None, compatibility: str | None = None) -> dict:
    s = load_session(project)
    if not s:
        raise JournalError("no change is open (use `change begin`)")
    if reason:
        s["reason"] = reason
    if compatibility:
        s["compatibility"] = compatibility
    kind = project.kind(s["kind"])
    ops, _ = _plan(kind, s)
    recs = read_records(kind)
    rec = {"id": new_id(), "after": recs[-1]["id"] if recs else None, "record_version": RECORD_VERSION,
           "type": "amend" if s.get("amends") else "change"}
    if s.get("amends"):
        rec["amends"] = s["amends"]
    rec.update({"class": s["class"], "target": s["target"], "options": s.get("options") or {},
                "reason": s.get("reason") or "", "compatibility": s.get("compatibility") or "unknown",
                "ops": ops})
    append_record(kind, rec)
    _save_session(project, None)
    return rec


def abort(project: Project) -> dict | None:
    s = load_session(project)
    _save_session(project, None)
    return s


def reopen(project: Project, kind: Kind, cid: str) -> dict:
    eff = effective(read_records(kind))
    ids = [e["id"] for e in eff]
    if cid not in ids:
        raise JournalError(f"no change {cid!r} in the unreleased journal (released changes are frozen)")
    _, touched = replay(kind, eff)
    i = ids.index(cid)
    later = [e["id"] for e in eff[i + 1:] if D.overlaps(touched[cid], touched[e["id"]])]
    if later:
        raise JournalError(f"cannot reopen {cid}: later change(s) {later} touch the same schema locations; "
                           "reopen those first, or record a new change instead")
    e = eff[i]
    return begin(project, kind, e["class"], e["target"], e.get("options") or {}, e.get("reason", ""),
                 e.get("compatibility"), amends=cid)


def drop(kind: Kind, cid: str) -> dict:
    recs = read_records(kind)
    if cid not in [e["id"] for e in effective(recs)]:
        raise JournalError(f"no change {cid!r} in the unreleased journal")
    rec = {"id": new_id(), "after": recs[-1]["id"] if recs else None, "record_version": RECORD_VERSION,
           "type": "drop", "drops": cid}
    append_record(kind, rec)
    return rec


# --- check (CI gate) ---------------------------------------------------------------

def _forks(records: list[dict], touched: dict[str, set[str]]) -> list[str]:
    target_of = {r["id"]: r.get("amends") or r.get("drops") or r["id"] for r in records}
    children: dict[Any, list[str]] = {}
    for r in records:
        children.setdefault(r.get("after"), []).append(r["id"])
    known = {r["id"] for r in records}
    problems = []
    for r in records:
        if r.get("after") is not None and r["after"] not in known:
            problems.append(f"record {r['id']} follows unknown record {r['after']} (journal truncated by a "
                            "release on another branch? rebase onto the release)")

    def desc(i):
        out, stack = set(), [i]
        while stack:
            x = stack.pop()
            out.add(x)
            stack += children.get(x, [])
        return out

    for parent, kids in children.items():
        if len(kids) < 2:
            continue
        segs = [desc(k) for k in kids]
        for x in range(len(kids)):
            for y in range(x + 1, len(kids)):
                ta = set().union(*(touched.get(target_of[i], set()) for i in segs[x]))
                tb = set().union(*(touched.get(target_of[i], set()) for i in segs[y]))
                if D.overlaps(ta, tb):
                    problems.append(
                        f"records {kids[x]} and {kids[y]} were written concurrently (both follow "
                        f"{parent}) and touch overlapping schema locations; check their order, then set "
                        f"the later one's \"after\" to the other branch's last record")
    return problems


def pending_fixture_problems(kind: Kind, eff: list[dict], work: dict) -> list[str]:
    h = head(kind)
    d = kind.fixtures / h if kind.fixtures else None
    if not d or not d.is_dir():
        return []
    f_from = kind.version_field(h)
    managed = [o["name"] for e in eff for o in e["ops"] if o.get("op") == "add-field" and o.get("managed")]
    f_to = managed[-1] if managed else f_from
    fields = {f for f in (f_from, f_to) if f}
    out = []
    for p in sorted(d.glob("*.json")):
        doc = load_json(p)
        if f_from and isinstance(doc, dict):
            doc = {k: v for k, v in doc.items() if k != f_from}
        schema = kind.schemas()[h]
        ctx = O.Ctx(h, "unreleased", kind.root)
        try:
            for e in eff:
                for op in parse_entry_ops(kind, e):
                    nxt = op.apply_schema(schema)
                    doc = op.apply_instance(schema, doc, ctx)
                    schema = nxt
        except O.MigrationError as err:
            out.append(f"fixture {p.name}: pending changes cannot migrate it: {err}")
            continue
        errs = errors(relaxed(work, {"#": fields}), doc, limit=3)
        if errs:
            out.append(f"fixture {p.name}: after pending changes it is invalid under the working schema: {errs[0]}")
    return out


def check(project: Project, kind: Kind) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    try:
        recs = read_records(kind)
        eff = effective(recs)
        expected, touched = replay(kind, eff)
    except (O.DslError, SchemaError) as e:
        afters = [r.get("after") for r in recs] if "recs" in locals() else []
        if len(afters) != len(set(afters)):
            e = f"{e}\n  (the journal contains concurrently written records from a merge; their order may be the cause)"
        return [str(e)], warns
    errs += _forks(recs, touched)
    work = working(kind)
    want_id = schema_id(kind.name, head(kind))
    if work.get("$id") != want_id:
        errs.append(f"working schema $id is {work.get('$id')!r}, expected {want_id!r} (head of the chain)")
    res = D.residual(expected, work)
    if res:
        errs.append("working schema has changes not explained by the journal:\n  " + "\n  ".join(res))
    else:
        errs += pending_fixture_problems(kind, eff, work)
    s = load_session(project)
    if s and s["kind"] == kind.name:
        warns.append(f"change open: {s['class']} {s['target']}" + (f" (amending {s['amends']})" if s.get("amends") else ""))
    return errs, warns


# --- release ---------------------------------------------------------------------

_ID = re.compile(r'("\$id"\s*:\s*")urn:[^"]*(")')


def release(project: Project, kind: Kind, version: str, compatibility: str | None = None,
            description: str = "") -> Path:
    if load_session(project):
        raise JournalError("a change is open; commit or abort it before releasing")
    errs, _ = check(project, kind)
    if errs:
        raise JournalError("check fails; fix before releasing:\n  " + "\n  ".join(errs))
    eff = effective(read_records(kind))
    if not eff:
        raise JournalError("nothing to release: the journal has no changes")
    h = head(kind)
    if vkey(version) <= vkey(h):
        raise JournalError(f"release version {version} must be greater than {h}")

    claims = [e.get("compatibility") or "unknown" for e in eff]
    compat = compatibility or max(claims, key=COMPAT_ORDER.index)
    managed = [o["name"] for e in eff for o in e["ops"] if o.get("op") == "add-field" and o.get("managed")]
    rel_dir = kind.migrations
    snap = kind.root / "releases" / f"{kind.name}-{version}.schema.json"

    ops = []
    for e in eff:
        for o in copy.deepcopy(e["ops"]):
            if o.get("op") == "adapter":
                o["module"] = os.path.relpath(kind.root / o["module"], rel_dir)
            ops.append(o)
    body: dict = {"record_version": RECORD_VERSION, "from": h, "to": version, "document_kind": kind.name}
    if description:
        body["description"] = description
    body["compatibility"] = compat
    if managed:
        body["version_path"] = "/" + managed[-1]
    body["changes"] = [{"id": e["id"], "class": e["class"], "target": e["target"],
                        "reason": e.get("reason", ""), "compatibility": e.get("compatibility") or "unknown",
                        **({"options": e["options"]} if e.get("options") else {}),
                        "operations": len(e["ops"])}
                       for e in eff]
    body["schema_snapshot"] = os.path.relpath(snap, rel_dir)
    body["operations"] = ops

    from .records import validate
    bad = validate("release_record", body)
    if bad:
        raise JournalError("internal: release record fails its schema: " + "; ".join(bad))
    text = kind.schema_file.read_text(encoding="utf-8")
    new_text, n = _ID.subn(lambda m: m.group(1) + schema_id(kind.name, version) + m.group(2), text, count=1)
    if n != 1:
        raise JournalError(f"cannot find the $id line in {kind.schema_file}")
    record = rel_dir / f"{len(kind.chain()) + 1:04d}-{h}-to-{version}.yaml"
    journal_text = kind.journal.read_text(encoding="utf-8")

    written = []
    try:
        snap.parent.mkdir(parents=True, exist_ok=True)
        rel_dir.mkdir(parents=True, exist_ok=True)
        for path, content in ((snap, new_text), (record, yaml.safe_dump(body, sort_keys=False, width=100)),
                              (kind.schema_file, new_text), (kind.journal, "")):
            written.append((path, path.read_text(encoding="utf-8") if path.exists() else None))
            path.write_text(content, encoding="utf-8")
        kind._chain = kind._schemas = None
        kind.schemas()  # verifies the new edge replays to its snapshot
    except Exception:
        for path, old in reversed(written):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(old, encoding="utf-8")
        kind._chain = kind._schemas = None
        raise
    del journal_text
    return record

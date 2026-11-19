"""Project config, changeset (DSL) loading, the linear version chain and
schema generation."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import ops as O
from .schema import SchemaError, check_metaschema, check_supported, parse_id, schema_id

COMPAT = {"backward-compatible", "breaking", "unknown"}
MANUAL_MIGRATION = {"unsupported", "partial", "manual"}
_UNSET = object()


def vkey(v: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(x) for x in v.split("."))
    except ValueError:
        raise O.DslError(f"version {v!r} is not x.y.z") from None
    if len(parts) != 3:
        raise O.DslError(f"version {v!r} is not x.y.z")
    return parts


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


@dataclass
class Changeset:
    file: Path
    from_: str
    to: str
    kind: str
    document_kind: str
    description: str = ""
    compatibility: str = "unknown"
    version_path: Any = _UNSET
    operations: list[O.Operation] = field(default_factory=list)
    # manual-baseline only
    schema_file: Path | None = None
    reason: str = ""
    instance_migration: str = "unsupported"
    adapter: O.Adapter | None = None
    preserve_paths: list[str] = field(default_factory=list)
    invalidate_paths: list[str] = field(default_factory=list)
    # journal-released declarative edges: the hand-edited schema is authoritative and the
    # operations must replay to it (modulo annotations)
    snapshot_file: Path | None = None
    changes: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.document_kind} {self.from_} -> {self.to}"

    def assessment(self) -> dict:
        """Separate dimensions: implementation / instance migration / compatibility claim."""
        if self.kind == "manual-baseline":
            impl = "manual-baseline"
            mig = self.instance_migration + (" (custom adapter)" if self.adapter else "")
        else:
            impl = "declarative" + (" + escape hatch" if any(o.escape_hatch for o in self.operations) else "")
            effects = {o.instance_effect() for o in self.operations}
            if O.ADAPTER in effects:
                mig = "automatic (includes custom adapter)"
            elif O.LOSSY in effects:
                mig = "automatic, LOSSY (approved in changeset)"
            elif O.REWRITE in effects:
                mig = "automatic"
            elif O.VALIDATED in effects:
                mig = "none needed if documents validate (some ops only tighten/replace schema)"
            else:
                mig = "none needed"
        return {"implementation": impl, "instance_migration": mig, "compatibility_claim": self.compatibility}

    def claim_problems(self) -> tuple[list[str], list[str]]:
        """(errors, warnings) for the reviewer's compatibility claim vs. what the ops do.

        backward-compatible := every document valid under `from` is valid, unchanged, under `to`.
        """
        errs, warns = [], []
        if self.compatibility != "backward-compatible":
            return errs, warns
        if self.kind == "manual-baseline":
            warns.append("backward-compatible claim on a manual baseline is the reviewer's assertion only; "
                         "the tool cannot check it beyond fixtures")
            return errs, warns
        for i, o in enumerate(self.operations):
            eff = o.instance_effect()
            if eff in (O.REWRITE, O.LOSSY, O.ADAPTER):
                errs.append(f"op #{i + 1} ({o.describe()}) rewrites documents, contradicting the "
                            "backward-compatible claim")
            elif eff == O.VALIDATED:
                warns.append(f"op #{i + 1} ({o.describe()}) may reject old documents; claim is checked "
                             "only against registered fixtures")
        return errs, warns


CS_KEYS = {"from", "to", "kind", "document_kind", "description", "compatibility", "version_path",
           "operations", "schema", "reason", "instance_migration", "adapter", "preserve_paths",
           "invalidate_paths", "schema_snapshot", "changes", "record_version"}


def load_changeset(path: Path) -> Changeset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise O.DslError(f"{path}: changeset must be a mapping")
    unknown = set(raw) - CS_KEYS
    if unknown:
        raise O.DslError(f"{path}: unknown key(s) {sorted(unknown)}")
    if "record_version" in raw:  # tool-written release record: validate against the record schema
        from .records import validate
        bad = validate("release_record", raw)
        if bad:
            raise O.DslError(f"{path}: invalid release record: " + "; ".join(bad))
    for k in ("from", "to", "document_kind"):
        if k not in raw:
            raise O.DslError(f"{path}: missing '{k}'")
    from_, to = str(raw["from"]), str(raw["to"])
    if vkey(to) <= vkey(from_):
        raise O.DslError(f"{path}: 'to' ({to}) must be greater than 'from' ({from_})")
    kind = raw.get("kind", "declarative")
    compat = raw.get("compatibility", "unknown")
    if compat not in COMPAT:
        raise O.DslError(f"{path}: compatibility must be one of {sorted(COMPAT)}")
    cs = Changeset(path, from_, to, kind, raw["document_kind"], str(raw.get("description", "")).strip(), compat,
                   raw.get("version_path", _UNSET))
    base = path.parent
    try:
        if kind == "declarative":
            bad = {"schema", "instance_migration", "adapter", "preserve_paths", "invalidate_paths"} & set(raw)
            if bad:
                raise O.DslError(f"keys {sorted(bad)} are only valid on manual-baseline edges")
            if not raw.get("operations"):
                raise O.DslError("declarative changeset needs a non-empty 'operations' list")
            cs.operations = [O.parse_op(s, base) for s in raw["operations"]]
            if "schema_snapshot" in raw:
                cs.snapshot_file = (base / raw["schema_snapshot"]).resolve()
            cs.changes = list(raw.get("changes", []))
        elif kind == "manual-baseline":
            if "operations" in raw:
                raise O.DslError("manual-baseline edges cannot have operations (the schema file is the result)")
            for k in ("schema", "reason"):
                if k not in raw:
                    raise O.DslError(f"manual-baseline needs '{k}'")
            cs.schema_file = (base / raw["schema"]).resolve()
            cs.reason = str(raw["reason"]).strip()
            cs.instance_migration = raw.get("instance_migration", "unsupported")
            if cs.instance_migration not in MANUAL_MIGRATION:
                raise O.DslError(f"instance_migration must be one of {sorted(MANUAL_MIGRATION)}")
            if "adapter" in raw:
                if cs.instance_migration != "manual":
                    raise O.DslError("an adapter requires instance_migration: manual")
                cs.adapter = O.Adapter({"op": "adapter", **raw["adapter"],
                                        "reason": raw["adapter"].get("reason", cs.reason)}, base)
            elif cs.instance_migration == "manual":
                raise O.DslError("instance_migration: manual needs an 'adapter'")
            cs.preserve_paths = list(raw.get("preserve_paths", []))
            cs.invalidate_paths = list(raw.get("invalidate_paths", []))
        else:
            raise O.DslError(f"kind must be 'declarative' or 'manual-baseline', got {kind!r}")
    except O.DslError as e:
        raise O.DslError(f"{path}: {e}") from None
    return cs


@dataclass
class Kind:
    name: str
    root: Path
    baseline: Path
    migrations: Path
    generated: Path
    version_path: str | None
    fixtures: Path | None = None
    schema_file: Path | None = None   # working (hand-edited) schema, journal mode
    journal: Path | None = None
    _chain: list[Changeset] | None = None
    _schemas: dict[str, dict] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def baseline_schema(self) -> dict:
        return load_json(self.baseline)

    @property
    def baseline_version(self) -> str:
        k, v = parse_id(self.baseline_schema.get("$id", ""))
        if k != self.name:
            raise SchemaError(f"baseline {self.baseline} has kind {k!r}, expected {self.name!r}")
        return v

    def chain(self) -> list[Changeset]:
        if self._chain is not None:
            return self._chain
        edges = [load_changeset(p) for p in sorted(self.migrations.glob("*.yaml"))]
        by_from: dict[str, Changeset] = {}
        for e in edges:
            if e.document_kind != self.name:
                raise O.DslError(f"{e.file}: document_kind {e.document_kind!r} != {self.name!r}")
            if e.from_ in by_from:
                raise O.DslError(f"two edges leave {e.from_}: {by_from[e.from_].file.name} and {e.file.name} "
                                 "(branching histories are not supported)")
            by_from[e.from_] = e
        out, cur = [], self.baseline_version
        while cur in by_from:
            e = by_from.pop(cur)
            out.append(e)
            cur = e.to
        if by_from:
            raise O.DslError(f"edges not reachable from baseline {self.baseline_version}: "
                             f"{[e.file.name for e in by_from.values()]}")
        self._chain = out
        return out

    def versions(self) -> list[str]:
        return [self.baseline_version] + [e.to for e in self.chain()]

    def version_field(self, version: str) -> str | None:
        vp = self.version_path
        for e in self.chain():
            if vkey(e.to) > vkey(version):
                break
            if e.version_path is not _UNSET:
                vp = e.version_path
        if vp is None:
            return None
        if not (vp.startswith("/") and vp.count("/") == 1):
            raise O.DslError(f"version_path {vp!r}: only top-level '/<field>' is supported")
        return vp[1:]

    def edge(self, from_: str) -> Changeset:
        for e in self.chain():
            if e.from_ == from_:
                return e
        raise O.DslError(f"no edge leaves version {from_}")

    def schemas(self) -> dict[str, dict]:
        """Generate every version's schema by replaying the chain."""
        if self._schemas is not None:
            return self._schemas
        base = self.baseline_schema
        check_metaschema(base)
        self.warnings += [f"baseline: {w}" for w in check_supported(base)]
        out = {self.baseline_version: base}
        cur = base
        for e in self.chain():
            errs, warns = e.claim_problems()
            if errs:
                raise O.DslError(f"{e.file.name}: " + "; ".join(errs))
            self.warnings += [f"{e.file.name}: {w}" for w in warns]
            if e.kind == "manual-baseline":
                nxt = load_json(e.schema_file)
                check_metaschema(nxt)
                if nxt.get("$id") != schema_id(self.name, e.to):
                    raise SchemaError(f"{e.file.name}: manual baseline {e.schema_file.name} has $id "
                                      f"{nxt.get('$id')!r}, expected {schema_id(self.name, e.to)!r}")
                try:
                    check_supported(nxt)
                except SchemaError as err:
                    self.warnings.append(f"{e.file.name}: manual baseline uses constructs later "
                                         f"declarative edges cannot operate on: {err}")
            else:
                check_supported(cur)
                nxt = cur
                for i, op in enumerate(e.operations):
                    try:
                        nxt = op.apply_schema(nxt)
                        check_supported(nxt)
                    except (O.DslError, SchemaError) as err:
                        raise O.DslError(f"{e.file.name} op #{i + 1} ({op.op}): {err}") from None
                    self.warnings += [f"{e.file.name} op #{i + 1}: {n}" for n in op.notes]
                    if isinstance(op, O.AddField) and op.spec.get("managed"):
                        if op.spec["at"] not in ("$", "#") or op.spec["name"] != self.version_field(e.to):
                            raise O.DslError(f"{e.file.name} op #{i + 1}: managed field must be the "
                                             f"top-level version field {self.version_field(e.to)!r}")
                nxt = {**nxt, "$id": schema_id(self.name, e.to)} if "$id" in nxt else \
                    {"$id": schema_id(self.name, e.to), **nxt}
                check_metaschema(nxt)
                if e.snapshot_file:
                    from .schemadiff import equivalent, residual
                    snap = load_json(e.snapshot_file)
                    if snap.get("$id") != schema_id(self.name, e.to):
                        raise SchemaError(f"{e.file.name}: snapshot $id {snap.get('$id')!r} != "
                                          f"{schema_id(self.name, e.to)!r}")
                    if not equivalent(nxt, snap):
                        raise O.DslError(f"{e.file.name}: operations do not replay to the schema snapshot "
                                         f"{e.snapshot_file.name}:\n  " + "\n  ".join(residual(nxt, snap)))
                    check_metaschema(snap)
                    nxt = snap
            out[e.to] = nxt
            cur = nxt
        self._schemas = out
        return out

    def generated_path(self, version: str) -> Path:
        return self.generated / f"{self.name}-{version}.schema.json"


class Project:
    def __init__(self, config: Path):
        self.config = config.resolve()
        self.root = self.config.parent
        raw = yaml.safe_load(self.config.read_text())
        self.kinds: dict[str, Kind] = {}
        for name, k in (raw.get("kinds") or {}).items():
            r = self.root
            self.kinds[name] = Kind(name, r, r / k["baseline"], r / k["migrations"], r / k["generated"],
                                    k.get("version_path"), (r / k["fixtures"]) if k.get("fixtures") else None,
                                    (r / k["schema"]) if k.get("schema") else None,
                                    (r / k["journal"]) if k.get("journal") else None)

    def kind(self, name: str | None) -> Kind:
        if name is None:
            if len(self.kinds) == 1:
                return next(iter(self.kinds.values()))
            raise O.DslError(f"several document kinds; pass --kind ({sorted(self.kinds)})")
        if name not in self.kinds:
            raise O.DslError(f"unknown kind {name!r}; known: {sorted(self.kinds)}")
        return self.kinds[name]

    @staticmethod
    def find(start: Path) -> "Project":
        p = start.resolve()
        for d in [p, *p.parents]:
            if (d / "schemamig.yaml").is_file():
                return Project(d / "schemamig.yaml")
        raise O.DslError("no schemamig.yaml found (pass --project)")


def generate(kind: Kind, write: bool) -> list[tuple[Path, str]]:
    """Return [(path, status)] where status in {'ok','changed','new'}; write if asked."""
    res = []
    snap_versions = {e.to for e in kind.chain() if e.snapshot_file}
    for v, s in kind.schemas().items():
        if v == kind.baseline_version or v in snap_versions:
            continue
        path = kind.generated_path(v)
        text = dump_json(s)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        status = "ok" if old == text else ("new" if old is None else "changed")
        if write and status != "ok":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        res.append((path, status))
    return res


def deep_copy(x):
    return copy.deepcopy(x)

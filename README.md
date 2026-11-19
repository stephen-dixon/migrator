# schemamig: operation-sourced JSON Schema evolution (POC)

A throwaway proof of concept for `libtokamap_dsl_migration_poc_brief.md`. Schema changes
are written as reviewable operations in YAML changesets. The tool uses each changeset to
both **generate the next JSON Schema** and **migrate existing documents**. See
[EVALUATION.md](EVALUATION.md) for the findings.

```
baseline schema (hand-written snapshot)
   + changeset 1.0.0 -> 1.0.1  ──> generated schema 1.0.1   and   document migration 1.0.0 -> 1.0.1
   + changeset 1.0.1 -> 1.1.0  ──> generated schema 1.1.0   and   document migration ...
   + manual-baseline 1.2.0 -> 2.0.0 (hand-written schema; no automatic migration)
```

## Install / run

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q                       # 49 tests: unit (op semantics) + e2e (example project)

cd examples/tokamap
S=../../.venv/bin/schemamig
$S explain --kind tokamap-mappings        # per edge: ops, effects, classification
$S build --check                          # generated/ matches the changesets (CI gate)
$S verify                                 # fixtures valid, golden migrations match, compat claims hold
$S migrate fixtures/mappings/1.0.0/equilibrium.json --kind tokamap-mappings --dry-run
$S migrate fixtures/mappings/1.0.0/equilibrium.json --kind tokamap-mappings --out /tmp/eq.json
$S migrate fixtures/config/1.0.0/mappings.cfg.json --kind tokamap-config --from-version 1.0.0 --to 1.2.0 --dry-run
$S impact fixtures/config/1.0.0/*.json --kind tokamap-config --edge 1.2.0 --from-version 1.0.0
$S new --kind tokamap-mappings --to 2.2.0  # changeset skeleton at the head of the chain
$S lint-schema ../../tests/data/libtokamap-globals.schema.json   # shows an unsupported-construct error
```

## Example project (`examples/tokamap`)

The baselines are libtokamap's real `mappings.schema.json` and `mappings.cfg.schema.json`
(from `develop`), byte for byte except `$id`. The fixtures include the real
`magnetics/40/mappings.json`.

| kind | edge | what | mechanism |
|---|---|---|---|
| mappings | 1.0.0→1.0.1 | repair the mis-nested `additionalProperties` the linter found | `patch-schema` escape hatch |
| mappings | 1.0.1→1.1.0 | `units` on DATA_SOURCE; new `ERROR` map type | `add-field`, `add-def`, `add-variant` |
| mappings | 1.1.0→2.0.0 | `dim_probe`→`probe`; `EXPR`→`EXPRESSION`; `offset`/`scale`→`transform{}`; envelope `{format_version, kind, mappings}` | `rename-field`, `rename-value`, `nest-fields`, `wrap`, `add-field` |
| mappings | 2.0.0→2.1.0 | parse literal `slice` strings into `{start,stop,step}`, keep templated ones | `adapter` escape hatch (Python, versioned with the edge) |
| config | 1.0.0→1.1.0 | start versioning; `metadata.version`→`content_version`; lowercase selectors | `add-field managed: version`, `rename-field`, `rename-value` |
| config | 1.1.0→1.2.0 | drop `metadata.author` (lossy, approved); `experiment` required | `remove-field lossy`, `make-required` |
| config | 1.2.0→2.0.0 | partitions reshaped | `manual-baseline`, `instance_migration: unsupported`, impact report |

## Changeset format

```yaml
from: 1.1.0
to: 2.0.0
document_kind: tokamap-mappings
compatibility: breaking            # reviewer claim: backward-compatible | breaking | unknown (default)
version_path: /format_version      # optional: the version field moves (top-level only)
operations:
  - { op: rename-field, at: def:dimension, from: dim_probe, to: probe }
```

**Targets (`at`)** name a *schema location*: `def:name`, a pointer `#/$defs/x/...`, or an
instance-shaped path `$.metadata`, `$.partitions[].selector`, `$.*` (one map-key level).
Instance paths can't cross `oneOf`/`anyOf`; use `def:` for a variant. The tool co-walks each
document with the old schema, choosing union branches by validation. Every node governed by
the target location gets transformed, so all users of a shared `$def` migrate.

| op | schema | documents |
|---|---|---|
| `add-field` (`type`\|`schema`, `required`, `default`, `position`, `managed: version`) | add property (+required) | insert default if required; required without default is refused |
| `rename-field` | rename, keep position, update `required` | rename key in place; both present → conflict error; absent → no-op |
| `remove-field` (`lossy: true` to approve) | delete property | without approval, fail if any document has a value; with it, report every lost value |
| `make-required` / `make-optional` | edit `required` | insert default / fail if missing and no default |
| `set-field-schema` (`convert: to-number\|to-string\|to-array`) | replace property schema | convert values, or leave them and rely on validation |
| `rename-value` | edit `enum`/`const`/`default` | map exact values; also works on discriminators |
| `add-def`, `add-variant` | add `$defs` entry / union branch | none |
| `nest-fields` | move properties into a new sub-object | move keys |
| `wrap` | the location becomes `{into: <old>}`; `$defs` stay at the root | wrap the node |
| **`patch-schema`** (escape hatch) | raw RFC 6902 patch, `reason` required | none; validation is the safety net |
| **`adapter`** (escape hatch) | optional `schema_patch` | reviewed Python `migrate(doc, ctx)`; run twice to check it is deterministic |
| **`kind: manual-baseline`** (escape hatch) | hand-written schema becomes the new baseline (`$id` checked) | `unsupported` by default (refused); `manual` + `adapter` allowed; `impact` report from `preserve_paths`/`invalidate_paths` |

**Safety rules (all tested).** Validate before migration, after **every op**, and after every
edge. Fail on unknown or unsupported constructs, unknown keys, unknown or absent versions, a
kind mismatch, branching chains, conflicts, unapproved loss, or a required field without a
default. Documents already at the target version are an explicit no-op. Output never
overwrites the input or an existing file without `--force`. A `backward-compatible` claim is
rejected if any op rewrites documents, and is tested against fixtures by `verify`.
Generated schemas must match `build --check`. Unsupported schema keywords (`allOf`, `if`,
`not`, `prefixItems`, `unevaluated*`, remote `$ref`, …) are rejected with a reason.

## Layout

```
schemamig/   schema.py (subset + addressing)  walk.py (instance/schema co-walk)  ops.py (vocabulary)
             project.py (changesets, chain, generation)  migrate.py  impact.py  verify.py  cli.py
examples/tokamap/  schemamig.yaml  baselines/  migrations/<kind>/*.yaml (+ adapters/)  generated/  fixtures/<kind>/<version>/
tests/       test_ops.py (brief section 7 + vocabulary)  test_e2e.py (example project, CLI rails)
```

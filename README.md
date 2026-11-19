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
python3 -m venv .venv && .venv/bin/pip install -e '.[test,lsp]'
.venv/bin/pytest -q                       # 84 tests: ops, e2e, journal, record format, LSP engine, headless Neovim

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

## Journal workflow (iteration 2)

You edit the schema by hand, and record the intent of each edit as a small change
scope. The scopes accumulate in an append-only journal, and a release folds them into
one version step.

```bash
schemamig change begin add-field def:rule --kind tokamap-config --reason "..." --compat backward-compatible
#   ...edit schemas/tokamap-config.schema.json in your editor...
schemamig change status [--json]        # "waiting for your edit" / "ready" + inferred ops (for statuslines)
schemamig change commit                 # infer ops from the scoped diff, verify by replay, append to the journal
schemamig change record rename-value def:rule --name fallback --map error=raise --kind tokamap-config   # declare after editing
schemamig change list | reopen <id> | drop <id> | abort | classes
schemamig check                         # CI gate
schemamig release 2.1.0 --kind tokamap-config
```

- **Expected state is replayed, not stored.** The expected schema is always
  `replay(last release, journal)`. The session file (`.schemamig/session.json`, gitignored)
  only holds the declared class, target and metadata. You can therefore declare a change
  before or after editing, and a lost session can't corrupt anything.
- **Scoped inference.** The declared class (`change classes`) narrows the diff, and the
  tool fills in the operation's parameters. A full replay must then match the working schema,
  so an edit the scope doesn't explain blocks the commit and is listed.
- **Explicit beats inferred.** Explicit parameters (`--from/--to`, `--map OLD=NEW`,
  `--fields/--into`, `--name`) always win, and inference only fills in what you leave out.
  Replay checks the result either way. Ambiguous cases, such as several enum values
  changing at once, require the explicit form. Note that some intent can't be seen in a
  diff at all: a swap of two enum values leaves the same set, so only the record says what
  happened.
- **Annotations are free.** `description`, `title`, `$comment`, `examples`, key order and
  the order of `required`/`enum`/`oneOf` can't affect validation. They never need a record.
- **Journal** (`changes/<kind>.jsonl`): one JSON record per line. Each record has an `id`
  and the `after` id it was written on top of. Amend and drop are new records, never
  edits. `.gitattributes` sets `merge=union`. `check` detects records written concurrently
  on different branches (same `after`). It flags them only if they touch overlapping schema
  locations, and you resolve that by re-pointing the later record's `after`.
- **`check`** requires the journal to replay cleanly to the working schema. It also runs
  the pending changes over the head-version fixtures and validates them against the working
  schema, and checks generated schemas and golden migrations (`verify`).
- **`release`** writes a release record (`changes:` is the changelog, then `operations`)
  and a schema snapshot. The snapshot is the working file byte for byte, with only `$id`
  bumped. It then truncates the journal. When the chain loads, each operation sequence
  must replay to its snapshot, so editing a snapshot by hand is detected.

### Auditing and the record format

`schemamig change show <id>` answers "does this record reflect what I meant?". It works
for unreleased changes and for changes already in a release. It shows:
- what you declared versus what the tool inferred
- one sentence per operation
- the schema diff the change accounts for
- the before/after effect on each fixture document

Journal and release records carry `record_version` and are validated against
`schemamig/record.schema.json`. That schema is generated from the operation classes, so
the vocabulary and the format can't drift (`schemamig record-schema --check`).

## Editor integration

`schemamig lsp` is a language server on stdio, built on pygls. It works only on the files
configured as working schemas:
- **Diagnostics:** every validation-relevant edit the journal doesn't explain is a warning
  where you made it. It uses the unsaved buffer, so you see it as you type. An open change
  shows as an info line with what it would record.
- **Code actions:**
  - *record as <class>*: offered only for classes whose dry run verifiably explains the edit
  - *record as unclassified*
  - *begin <class>* on the property under the cursor
  - commit / abort / status for the open change
- **Commands:** `schemamig.begin|record|commit|abort|status|list|show`. Arguments the
  client should prompt for are flagged `prompt_reason`.
- **Notification:** `schemamig/status`, for statuslines.
- **Saving:** recording is refused while the working schema has unsaved edits, so the
  journal can never get ahead of the file. Both clients save first.

**Neovim (0.10+)** (`editors/nvim`, tested headlessly against the real server in
`tests/test_nvim_smoke.py`):

```lua
vim.opt.rtp:prepend("/path/to/migrator/editors/nvim")
require("schemamig").setup({ cmd = { "/path/to/.venv/bin/schemamig", "lsp" } })
vim.o.statusline = "%f %{v:lua.require'schemamig'.statusline()}"
-- :SchemamigBegin <class> <target> [reason]  :SchemamigCommit  :SchemamigAbort
-- :SchemamigList  :SchemamigShow <id>  :SchemamigStatus   + vim.lsp.buf.code_action()
```

**VS Code** (`editors/vscode`, **untested**: syntax-checked only). Run `npm install` in
that folder, then load it as an unpacked extension. Set `schemamig.serverCommand` if
`schemamig` isn't on your PATH. Code actions work as above, a status-bar item shows the
open change, and there are palette commands for begin, commit, abort, list and show.

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
             journal.py (change scopes, check, release)  schemadiff.py  records.py (+ record.schema.json)
             audit.py (change show)  jsonpos.py  lsp.py
editors/     nvim/ (Lua plugin + headless smoke test)  vscode/ (thin client, untested)
examples/tokamap/  schemamig.yaml  baselines/  migrations/<kind>/*.yaml (+ adapters/)  generated/  fixtures/<kind>/<version>/
tests/       test_ops.py (brief section 7 + vocabulary)  test_e2e.py (example project, CLI rails)
```

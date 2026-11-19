# libtokamap: DSL-driven schema evolution — proof-of-concept brief

**Status:** Exploratory implementation brief for a local coding agent  
**Goal:** Test whether *authoring schema changes as explicit operations* is a better developer workflow than manually editing JSON Schema and writing corresponding instance migrations.  
**Scope:** A small, disposable proof of concept; **not** a production migration framework.

## 1. Concept to test

Treat a sequence of **declarative, human-reviewed changesets** as the authoritative description of schema evolution:

```text
baseline JSON Schema (v1)
          +
  change v1 -> v2 ──> generated JSON Schema (v2)
          │
          └──────────> migration of existing mapping documents (v1 -> v2)
          +
  change v2 -> v3 ──> generated JSON Schema (v3)
          │
          └──────────> migration of existing mapping documents (v2 -> v3)
```

The developer writes an operation such as `rename-field`, rather than independently editing the schema and the data. A tool can expose these as CLI commands, but storing operations in a reviewable DSL file is the essential feature. The CLI is merely a convenient authoring front end.

**Important:** Keep *schema transformation* separate from *instance transformation*. Adding an optional field changes the schema but need not rewrite existing files. A rename changes both. Adding a required field without a default is not automatically migratable.

## 2. Core hypothesis / questions

Evaluate whether this model:

1. Makes the author's intent clearer than a schema diff.
2. Reliably produces both an updated JSON Schema and an appropriate transformation of existing mapping documents.
3. Preserves human-readable, hand-editable mapping JSON, without generated timestamps or incidental rewrites.
4. Supports a clean escape hatch for changes that cannot be expressed declaratively.
5. Avoids an excessively complicated DSL or JSON Schema manipulation engine.

**Success is a reasoned assessment of the model**, not the number of features implemented.

## 3. Deliberately narrow implementation

Use a **small, representative subset** of libtokamap's JSON Schema (objects, `properties`, `required`, primitive field types, and simple nested paths). Do not attempt to support arbitrary JSON Schema composition (`oneOf`, `$ref`, `patternProperties`, conditional schemas, etc.) in this POC. Detect unsupported constructs and fail explicitly.

Use a simplified fixture modeled on the mapping document format; do not refactor production schemas, validators, or mapping loaders as part of the experiment. The old repo's detailed schema structure should be inspected locally before choosing representative fixtures. The original repository ZIP was not available for reinspection in the discussion that produced this brief.

Suggested operations:

| Operation | Schema effect | Instance migration | Default safety |
|---|---|---|---|
| `add-field` (optional) | Add a property | No change needed | Safe for existing instances |
| `rename-field` | Rename a property and update `required` | Rename key if present | Safe if destination absent; otherwise conflict |
| `remove-field` | Delete a property | Explicitly remove **only with approval**, or fail | Potentially lossy |
| `add-field` (required + default) | Add property and mark required | Insert default if missing | Possible if default valid |

If time is tight, implement only **optional add** and **rename**, and use the other two as unsupported-case demonstrations.

### Example authored DSL

YAML is illustrative; JSON or TOML is equally acceptable. Prefer a tiny typed internal representation over a sophisticated custom language.

```yaml
from: "1.0.0"
to: "1.1.0"
document_kind: "tokamap.mappings"
operations:
  - op: add-field
    path: "$.mappings.*.description"
    type: string
    required: false
  - op: rename-field
    from: "$.mappings.*.source"
    to: "$.mappings.*.data_source"
```

For the POC, it is fine to use literal, non-wildcard paths if wildcard paths make the engine significantly harder. If supporting `*`, define that it matches **one object-key level**; don't quietly introduce a general JSONPath implementation. Choose a fixture where these fields appear in explicit object schemas (not unhandled `patternProperties`).

Example CLI (interface exploratory, not a requirement):

```bash
tokamap-migrator apply-schema schemas/v1.json migrations/1.0.0-to-1.1.0.yaml --out schemas/v1.1.json
tokamap-migrator migrate examples/old.json --changes migrations/1.0.0-to-1.1.0.yaml --out examples/new.json
tokamap-migrator check --schema schemas/v1.1.json --document examples/new.json
```

Optional convenience command: `tokamap-migrator add-field ...` creates/appends an operation to the DSL file; it should **not** silently mutate the canonical schema without recording that operation.

## 4. Baselines and the escape hatch

The initial schema is an **explicit baseline snapshot**. A DSL history evolves it. When the DSL cannot express a change, support an explicitly recorded **manual baseline transition**:

```yaml
from: "1.1.0"
to: "2.0.0"
kind: manual-baseline
schema: "schemas/v2.0.0.json"
reason: "Reworked mapping shape; unsupported by POC operation set"
instance_migration: unsupported
```

The tool accepts the manually authored target schema as a *new starting point* after checking its validity and identity/version. It must **not** invent a document transformation or assert compatibility from a structural comparison.

### Compatibility is separate from expressibility

**An operation being outside the DSL is not, by itself, proof of a breaking change.** It means *the tool cannot prove or perform the change*. Likewise, a DSL-expressible removal may be a real breaking change. Represent these as separate dimensions:

- **Implementation:** declarative / manual-baseline.
- **Instance migration:** automatic / manual / partial / unsupported.
- **Compatibility claim:** explicitly reviewed as backward-compatible / breaking / unknown (depending on which direction of compatibility is meant).

For the POC, default every manual-baseline transition to **unknown compatibility and no automatic migration** until a reviewer explicitly supplies a policy. A version-major bump can be the conservative release convention, but not a substitute for the compatibility assessment.

Optional best-effort annotation:

```yaml
from: "1.1.0"
to: "2.0.0"
kind: manual-baseline
schema: "schemas/v2.0.0.json"
instance_migration: partial
preserve_paths:
  - "$.metadata"
  - "$.mappings.*.description"
invalidate_paths:
  - "$.mappings.*.legacy_source"
```

**Do not implement automatic deletion from these annotations in the first POC.** They can drive a *migration report* (preserved / needs review / invalidated), and optionally explicit, opt-in migration hooks later. `preserve_paths` means reviewed claims, not a guarantee that data can be copied without inspecting target structure. Reject unknown/unclassified fields rather than silently discarding them. A hand-written migration adapter, if eventually supported, should be versioned, tested, and explicitly attached to the edge.

## 5. Version model and safety rules

- Each change is a directed `from -> to` edge. Start with a **linear chain**; don't build a generic migration DAG yet.
- Embed a `schema_version` in the **example instances** (or use an explicit caller-supplied source version for legacy examples). Keep content/release version separate.
- Check that the source schema version matches the changeset's `from` and the generated schema's version matches `to`.
- Fail on an unknown source version, absent edge, wrong document kind, ambiguous rename, missing required defaults, or collisions.
- Validate the instance against its old schema before migration and against its new schema afterwards.
- Migrate to a **new output file**, not in place. Do not overwrite by default; surface a readable change report.
- Preserve untouched mapping content. Formatting preservation is *nice to have*, not essential for the POC, but minimise needless changes.
- Do not generate or require edit timestamps, hashes, per-file content versions, or lockfiles in the human-authored mapping instances for this exercise.
- A migration of an instance already at the target version must be an explicit no-op or refusal, **never** apply the same operation twice.

## 6. Suggested implementation sequence

1. **Fixture**: establish minimal baseline schema and 2–3 representative mapping documents, valid under that schema.
2. **DSL reader**: parse a small typed set of operations; report unsupported operations/paths clearly.
3. **Schema engine**: apply `add-field` and `rename-field` to the baseline schema, updating `required` correctly.
4. **Instance engine**: derive corresponding instance transformations from the *same* authored operations.
5. **Verification**: validate before/after, check versions, and produce human-readable diff/report.
6. **Escape hatch**: implement `manual-baseline` registration, with unknown compatibility and unsupported migration by default; optionally generate a field-impact report.
7. **Evaluation**: compare authoring a change through the DSL versus manually modifying schemas and instances; capture what felt awkward.

Choose Python for rapid experimentation unless integration into the existing C++ tooling is itself a specific goal. Avoid introducing a new runtime dependency into libtokamap for the POC.

## 7. Minimum test cases

- Adding an optional field updates schema and leaves old instances valid without modifications.
- Renaming a nested field updates schema and instance; retains the value and updates `required`.
- Rename conflict (old and new keys both present) fails without data loss.
- Missing source property for rename behaves according to a documented policy (usually no-op for an optional property).
- Adding a required field without default is refused as an automatic migration.
- Lossy removal requires explicit opt-in and reports lost paths/values.
- Unsupported JSON Schema constructs are rejected with an actionable explanation.
- A manual baseline is accepted as a schema baseline but does not accidentally migrate instances.
- Invalid old data and invalid migrated data both fail validation.
- Chaining two migration edges produces the expected final schema and instance.

## 8. Deliverables for review

- Small standalone prototype source plus a README with install/run commands.
- One baseline JSON Schema, 2–3 changeset files, generated schemas and before/after mapping fixtures.
- Tests covering the cases above (mark any deliberately unimplemented feature clearly).
- A demonstration of `manual-baseline` and a *non-destructive* impact report.
- A short `EVALUATION.md` addressing:
  - Which operations were natural to express?
  - Where did the DSL become awkward or insufficient?
  - How hard was schema AST manipulation, particularly for actual libtokamap schema features?
  - Should the canonical source be **operations + baselines**, or should hand-edited schemas remain authoritative with assisted diff/classification?
  - Is the manual-baseline escape hatch comprehensible and safe?
  - Is a full generic migration framework worthwhile, or would a small operation set + manual adapters be simpler?

## 9. Explicit non-goals

No production libtokamap integration, no automatic migration on load, no Git rewriting, no cross-repository migration, no schema diff inference, no lockfile, no full SemVer compatibility engine, no multi-branch graph resolver, no custom expression language, and no unreviewed destructive transformations.

## 10. Decision at the end of the spike

Make a **go / modify / abandon** recommendation. In particular, judge whether authoring explicit changes is genuinely easier than schema-first editing for the sorts of changes libtokamap actually expects. A useful outcome could be a **hybrid**: author common changes via a DSL, allow manually authored new baselines for complex evolution, and optionally introduce a schema-diff assistant *later* to propose (but never silently approve) operations.

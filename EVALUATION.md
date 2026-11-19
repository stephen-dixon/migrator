# Evaluation: operations as the source of truth for libtokamap schemas

**Recommendation: MODIFY.** Keep the operation vocabulary and escape hatches for changes
that rewrite documents; that is where the approach clearly earns its keep. But **don't make
operations the only source of the schema text.** Keep a hand-edited schema snapshot per
version as the authoritative artefact, and have the changeset carry the document migration
plus the schema effect, which the tool *checks* against the snapshot rather than generating
it (details in section 6).

All findings come from running the POC against libtokamap's real `mappings.schema.json`,
`mappings.cfg.schema.json` and `magnetics/40/mappings.json` (`develop` branch).

## 1. What the real schemas required (and what the brief would have excluded)

The brief proposed rejecting `oneOf`, `$ref` and `patternProperties`. **libtokamap's mapping
schema is made of exactly those constructs.** The root is a `patternProperties` map
(`"^.*$"`) of `oneOf [string, number, $ref map_type]`, and `map_type` is a `oneOf` of five
`$ref` variants told apart by a `map_type` const. With that subset rejected, the tool could
not touch a single mapping field. Supporting them was the most important design decision.

What made it tractable: operations target **schema locations** (`def:data_source`), not
instance paths. A co-walker then finds which instance nodes each location governs, picking
union branches by validating against them. That costs about 90 lines (`walk.py`) and needs no
JSONPath engine. Everything else in the brief's exclusion list (`allOf`, conditionals,
`prefixItems`, remote refs, …) is still rejected with a reason, and nothing in libtokamap
needed it.

**The linter found real bugs in the inherited schemas:**
- `mappings.schema.json`: `"additionalProperties": false` sits *inside* `patternProperties`,
  so it is a regex entry that rejects keys containing "additionalProperties", not a keyword.
  Repaired in edge 0001 through the `patch-schema` escape hatch.
- `globals.schema.json`: `patternProperties → {"args": {"$ref"}}` uses `args` as a keyword.
  It constrains nothing. The tool refuses this schema (`tests/data/`, tested).
- All three schemas share `$id: "/schemas/toplevel"`. The tool requires unique
  `urn:<kind>:<x.y.z>` ids.

These are a good argument for *some* tooling regardless of the DSL decision.

## 2. Which operations were natural to express?

Natural, with clear intent and less work than hand-editing schema plus script:

| change | DSL | by hand |
|---|---|---|
| rename a variant field (`dim_probe`→`probe`) | 1 line | edit 2 places in the schema + a script that finds DIMENSION entries inside a pattern map inside a oneOf |
| rename a discriminator / enum values (`EXPR`→`EXPRESSION`, selector casing) | 1 line | same, and easy to miss `default` |
| envelope the bare root (`wrap` + `add-field kind/format_version`) | 3 ops | restructure the root, move `$defs`, write a script |
| approved lossy removal with a per-value loss report | 1 line | rarely done carefully |
| `required` + default | 1 line | 2 edits + script |

Edge 0003 shows it best: six ops in about 35 lines of YAML give a correct 2.0.0 schema and a
migration of the real magnetics file, every mapping key preserved, with a report of every
rewrite.

Awkward or no gain:
- **Additive changes** (`add-def` + `add-variant` for ERROR) are a schema fragment pasted
  into YAML. That is the same work as editing the schema, but with worse tooling (no schema
  IDE support) and worse output formatting (section 5).
- **Fixing a schema bug** has no natural operation, so it falls back to `patch-schema`, a
  raw RFC 6902 patch. That is fine as an escape hatch, but it shows that many real schema
  edits are just edits.
- **Order-sensitivity:** each op must keep the document valid on its own, because there is
  validation after every op. It gives excellent error messages ("op #3 broke it") but rules
  out transiently-invalid sequences.

## 3. Where the DSL became insufficient (escape hatches needed)

For libtokamap these are not rare:

1. **References between mappings.** `EXPR.parameters`, `DIMENSION.dim_probe` and
   `CUSTOM.inputs` hold *other mapping keys* as strings, and `{{ … }}` templates refer to
   globals. JSON Schema has no notion of references, so a structural operation can't rename a
   mapping key and update the things that point at it. This is the biggest conceptual
   limitation for this domain.
2. **Mapping keys are data with their own grammar** (`coil[#]/position[#]/r`). Changing that
   grammar, e.g. `[#]`→`[*]`, rewrites keys inside a pattern map. There is no op for it, and
   there shouldn't be: you need an adapter.
3. **Changes that depend on values** (split DATA_SOURCE by `data_source` value; turn
   `offset`/`scale` into an expression), or parsing string content (the `slice` example):
   adapter.
4. **Moves across objects or documents** (e.g. moving metadata between `mappings.cfg.json`
   and `globals.json`): there is no cross-document model.
5. **Missing simple ops** that would be cheap to add: `unnest`, `remove-variant`,
   `remove-def`, `rename-def`, move between sibling objects.

The `adapter` op (a Python function attached to the edge, schema effect as a patch, checked
for determinism, validated before and after) handled cases 1–3 cleanly. **The escape hatch
works.** But for libtokamap, a fair share of meaningful format changes will need it.

## 4. Is the manual-baseline escape hatch comprehensible and safe?

Yes; this was the most convincing part of the POC.
- Implementation, instance migration and compatibility are reported separately (`explain`).
  A manual baseline defaults to `unsupported` / `unknown`, and migration across it is
  refused with the stated reason.
- `impact` is non-destructive (tested by hashing the files). It migrates older documents *in
  memory* up to the edge. It classifies every top-level subtree as
  preserved/invalidated/needs-review, checks that preserved values are *valid at their new
  location*, and flags annotations that match nothing, which catches typos like `$.grups`.
- Attaching an adapter to a manual baseline (`instance_migration: manual`) lets the chain
  continue without pretending the change is declarative.
- A `backward-compatible` claim on a manual baseline is only a warning, but `verify` checks
  it against fixtures and catches it in the test.

One gap: `preserve_paths` is checked structurally, not semantically. "Preserved, value valid
there" doesn't prove the value *means* the same thing.

## 5. Quality of the generated schemas

Checked by inspecting `generated/` and with metrics:
- **Valid and deterministic.** Every generated schema passes the draft 2020-12 metaschema
  and the supported-subset check, and `build --check` reproduces it byte for byte. There
  are no orphaned `$defs`, `required` stays consistent with `properties` (rename and remove
  update it), and property and key order are preserved.
- **Formatting is lost (the main quality problem).** The hand-written baseline is 103 lines
  in compact style (`{ "type": "string" }`). Generated 1.0.1 is 192 lines. A 2-line semantic
  change shows up as a **165-line diff** against the baseline. After the first edge the
  diffs between generated versions are minimal, but the authoring style is gone for good.
  Number formatting drifts too (`1e-6` → `1e-06` in documents). A fixed compact printer
  would mostly solve this; true format preservation needs a CST-preserving JSON library.
- **Prose goes stale.** Annotations aren't migrated. The config schemas still say *"JSON
  Schema for v1.0 of default top-level globals"* at 1.2.0, and a renamed field's description
  can still describe the old name. Ops can't know this. Hand-edited schemas don't have the
  problem.
- **Tool-chosen shape.** `nest-fields` and `wrap` add `additionalProperties: false` and
  `type: object` to the objects they create. That is a sensible default, but it is the
  tool's style, not the author's.
- **Inherited weaknesses carry forward unchanged.** Open objects (`$defs/metadata` has no
  `additionalProperties: false`) are faithfully kept. This matters: under an open object, an
  *unmigrated* document with the old key still validates, so validation stops being a safety
  net. The tool warns on rename/remove into open objects; libtokamap should close these
  objects.
- **Runtime parity risk.** libtokamap validates with **valijson in draft-7 mode**, but the
  schemas declare 2020-12 and this tool validates with Python `jsonschema` as 2020-12. The
  supported subset is draft-7-safe today; `$ref` with siblings is the obvious divergence
  (warned). A real version needs a parity test against valijson, as the team proposal says.

## 6. Should the canonical source be operations + baselines, or hand-edited schemas?

The POC suggests a hybrid. The value is almost entirely on the **document-migration** side:
safe renames inside unions, conflict detection, loss reports, defaults, versioning. On the
schema side, generation costs formatting and prose, adds a second artefact (YAML plus a
generated file that must be kept in sync), and for additive changes it is a worse editor.

The suggested modification:
1. Schemas stay hand-edited, one snapshot per version (as the manual baselines are now).
2. Each edge's changeset lists only the ops needed to migrate documents (rename, nest,
   rename-value, remove, defaults, adapters). Purely additive edges may have no ops.
3. The tool replays the ops' schema effects on vN and **checks** the result against the
   hand-edited vN+1 *at the locations the ops touch*. It then migrates fixtures and
   validates them against vN+1. Hand edits outside the touched locations are allowed but
   listed for review. This keeps the safety properties shown here while removing
   `patch-schema`, `add-def` and the formatting problem.
4. A schema-diff assistant could later *propose* ops from the diff (e.g. "`dim_probe`
   disappeared and `probe` appeared in `$defs/dimension`: rename?"). It would never
   approve them.

This keeps nearly all of the engine built here (walker, ops' instance side, chain, verify,
impact, manual edges). Only `generate` turns into `check`.

## 7. Is a full generic migration framework worthwhile?

No. The vocabulary that paid off was about 8 ops, and the hard libtokamap cases (references,
key grammar, value-dependent changes) need adapters whatever the vocabulary is. A small op
set plus versioned, tested Python adapters on edges, plus manual baselines, covered
everything tried. Building a richer declarative language for the long tail would
re-implement Python badly.

## 8. Other limitations found (not addressed in the POC)

- **Effective-mapping equivalence isn't checked.** `verify` checks JSON shape and golden
  outputs. Renaming `EXPR`→`EXPRESSION` also needs the C++ loader to change in lockstep; the
  tool knows nothing about runtime capability. The proposal's "verify effective mapping
  behaviour" criterion needs libtokamap in the loop.
- **Version field in the payload namespace.** The bare mapping file's `"version"` key is also
  a valid *mapping* key (it matches `^.*$` as a string mapping). The engine has to strip it
  before the ops run and restamp it afterwards, and moving it needs a `managed: version`
  special case. This supports the envelope proposal.
- **Union branch selection by validation** is correct for `oneOf` on valid documents. Under
  `anyOf` or overlapping patterns a node can be governed by several locations and gets
  transformed by ops on any of them. This isn't reported as ambiguous. It is also roughly
  O(nodes × branches) validations per op: fine for current file sizes, untested on very
  large mapping sets.
- **Pointer targets into `oneOf/N`** are fragile (adding a variant earlier shifts the
  indices). Use named `$defs`.
- **JSON only.** libtokamap also loads TOML configs; the tool has no TOML round-trip.
- **Linear chains only, top-level version fields only, no downgrades.** All intentional, and
  all fail explicitly.
- **Adapters are arbitrary Python.** The determinism check and validation before and after
  are the only guards; review and fixtures carry the rest.

## Addendum: iteration 2, the journal workflow

This implements the hybrid from section 6. Schemas are hand-edited, and each edit's intent
is recorded as a granular change scope in an append-only JSON Lines journal, then folded at
release. Findings:

- **Equivalence with the YAML approach.** The mappings 1.1.0→2.0.0 release, re-authored as
  six journal scopes (rename, discriminator rename, nest, wrap, two adds), produces a schema
  equivalent to the YAML-generated one and **identical** migrations of both fixtures
  (`test_journal_reauthoring_matches_the_yaml_changeset`). The engine needed one extension:
  `nest-fields` accepts the hand-written container schema.
- **The formatting problem is solved.** A release snapshot is the working file byte for
  byte with only `$id` changed. A two-field release of the compact config schema is a
  3-line diff, against about 165 lines in iteration 1.
- **Inference is reliable when scoped.** With the class declared, each class needs only
  "exactly one key added/removed/changed at the target". Anything else blocks the commit
  with a pointer-level explanation. Pairing enum values by position is the one convention
  developers have to know.
- **Overlap detection is coarse and conservative.** It is diff-based: two scopes that both
  edit an object's `required` list are flagged as overlapping even when their order doesn't
  matter. Replay is the real backstop.
- **Rough edges.** Residual messages show normalised (sorted) enums. `check` stops at the
  first replay failure. A release on one branch truncates the journal, so branches still
  holding pre-release records must rebase. That is detected ("follows unknown record") but
  not resolved for you. Multiple schema kinds share one session at a time.

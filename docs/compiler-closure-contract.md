# Compiler-resolved test closure: contract request (MNCS-TEST-P-014)

`mncs-test-verify` reuses evidence across repository revisions today using a
host-measured source closure: every `.mncs` file under the obligation's
library roots plus the repo manifests, the verifier, and the ambient
stdlib authorities. That closure is a deliberate superset. It proves
documentation changes irrelevant, but it cannot prove one suite's edit
irrelevant to another obligation, nor one stdlib module's edit irrelevant
to a suite that never imports it. Both rerun conservatively (demonstrated:
a comment-only edit in `tests/closure_suite.mncs` re-executes the
self-suite obligation too).

Only the compiler knows the authoritative per-suite dependency closure.
This document defines the smallest stable compiler-owned contract that
would let `mncs-test` replace the superset with exact relevance.

## What mncs-test needs from `mncs test-inventory`

Extend the inventory report with a `resolution_closure` block describing
exactly what the compiler consulted to compile the suite:

```json
{
  "schema_version": "mncs.test-inventory/1",
  "inventory": {
    "subject_identity": "mncs:0.2:program:tests.self_suite",
    "resolution_closure": {
      "complete": true,
      "modules": [
        {
          "module": "mncs.test.assertions",
          "artifact_identity": "mncs:...:content-sha256:...",
          "source_path": "std/assertions.mncs"
        }
      ],
      "closure_digest": "sha256 over the sorted module entries",
      "compile_configuration": "profile + capability/experiment flags admitted"
    },
    "tests": [ "...unchanged..." ]
  }
}
```

Requirements on the block:

1. `modules` lists every module the resolver loaded for this suite,
   including the suite module itself and every transitively imported
   module actually contributing declarations (re-exports resolved).
2. `artifact_identity` is content-based: equal content implies equal
   identity regardless of checkout path, so closures compare across
   worktrees and revisions.
3. `complete: false` (or an absent block) means "the compiler cannot
   prove the closure" — test-side policy treats that as no closure
   and keeps the current superset fallback. Never silently truncate.
4. `closure_digest` is a single digest over the sorted module list so
   the host can compare closures without shipping file lists into
   native policy bounds.
5. `compile_configuration` binds every compile-time switch that can
   change codegen or test selection (profile, admitted experiments,
   capability gates). If none exist, a stable constant is fine.
6. Ordering is canonical (sorted by module name); unknown extra
   fields are ignored by the test consumer.

Explicitly NOT requested: per-test call graphs, per-test module
attribution, or coverage data. Suite-level closure is sufficient for
per-obligation invalidation; per-test selection can follow later.

## Open semantic question: `semantic_fingerprint` transitivity

Probed 2026-10-03 against the debug toolchain:

- per-test `subject_fingerprint` is body-insensitive (declaration
  identity): editing a test body leaves it unchanged.
- per-test `semantic_fingerprint` IS body-sensitive: `19 +% 23` to
  `19 +% 24` changes it.

Question for the compiler owner: does `semantic_fingerprint` cover
the transitive callee closure, or only the test's own declaration
body? `mncs-test` will not consume it for reuse decisions until its
exact coverage is specified; a wrong guess here would be an
unsoundness hole.

## Test-side consumption plan (no compiler patching in mncs-test)

When `resolution_closure` lands with `complete: true`:

1. Host binds `closure_digest` (+ `compile_configuration`) per
   obligation as a new bound field, replacing the host-measured
   source superset for obligations whose inventory carries it.
2. Native policy compares the compiler closure exactly like today's
   host closure (trusted + equal + fileset rule tag). The v2
   `closure_current` branch needs no structural change: only the
   measurement source changes.
3. Obligations without a closure block keep the host superset. Mixed
   repositories work: each obligation reuses at its own precision.
4. The host superset remains as the fail-closed fallback whenever
   `complete` is false or the block is absent (older toolchains).

Until then, `mncs-test` will NOT rediscover import semantics by
scanning `use` declarations. The superset stands.

## Fixtures proving the need

- `tests/closure_suite.mncs` + obligation
  `mncs-test.cross-module-suite-execution`: a second runnable suite.
  Edit either suite file and run `mncs-test-verify`: both
  obligations re-execute, because the shared superset covers both
  suites. With a compiler closure, only the edited suite's
  obligation would rerun.
- `tests/test_verifier_units.py` closure tests: pin the superset's
  soundness properties (stability, pruning, symlink handling,
  fail-closed bounds) so the fallback stays trustworthy.

## Coordination

- Owner: mncs-language (`test-inventory` report shape).
- mncs-test contact: this campaign (`spark-test-continuation`).
- Status: requested; superset fallback in production use.

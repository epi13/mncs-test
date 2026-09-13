# mncs-test architecture

## Layering

```text
MNCS source test functions
        ↓ typed TestResult / SuiteSummary
native/mncs/test modules
        ↓ current MNCS compiler/runtime
mncs-test transport adapter
        ↓ check-result/1 + evidence artifacts
mncs-actions and future Forge orchestration
```

The native layer owns assertion meaning, failure categories, deterministic
aggregation, bounded generation, replay inputs, and snapshot witnesses. It is
deliberately made from ordinary MNCS enums, records, functions, sequences,
bounded iteration, and arithmetic. There is no hidden registration table in
Python and no host-side assertion callback.

The adapter owns four boundaries that the current external-consumer profile
does not expose as native values:

1. TOML and controlled manifest/file discovery;
2. starting the `mncs` executable and enforcing an OS process timeout;
3. JSON/TOML transport to the current CLI and projection into the family
   `check-result/1` contract; and
4. byte-for-byte capture, hashing, and publication of raw artifacts.

The adapter receives a native result, validates its shape, and carries it
forward. A suite's returned verdict is authoritative; the adapter only checks
that the result can be transported and that its summary is consistent with
the declared test list.

## Manifest and discovery

The root `mncs-test.toml` is the default explicit suite. Direct
`tests/*.toml` files are also discoverable. Recursive filesystem search is
opt-in (`discover --recursive`) and is not used by `run` implicitly. This
keeps discovery deterministic and inspectable while the language/package
system develops a stronger native module discovery contract.

Each manifest names one MNCS source/module and may name one native suite
entrypoint. Entries are ordered, have stable IDs, and declare one of the
supported test kinds. Compile-pass, compile-fail, and diagnostic entries use
the compiler's structured validation result. Runtime entries use the MNCS
execution request ABI. `property` entries carry seed/case metadata but do not
ask Python to generate or check cases: the MNCS program does that.

## Execution sequence

For a manifest with a suite, the adapter first invokes the suite entrypoint,
then invokes each declared runtime entrypoint individually to preserve useful
per-test diagnostics. The suite result remains the semantic authority. This
currently recompiles the source at each process boundary; the duplicate work
is intentional evidence for the in-process/batch invocation pressure.

Compile-only entries invoke `mncs validate`. A compile-fail or diagnostic
entry is passing only when the compiler rejects the source and every declared
diagnostic prefix is present. This is a temporary structured-output adapter,
not string matching against human compiler prose.

The result has two layers:

- `mncs.test-result/1` carries detailed test outcomes, native results,
  classifications, diagnostics, identities, artifacts, and reproduction;
- `mncs.check-result/1` is a small action/Forge-facing envelope with the
  top-level verdict, claim, digest, unresolved list, and a reference to the
  detailed result.

## Failure taxonomy

`PASS`, `FAIL`, and `UNKNOWN` are intentionally not collapsed. `FAIL` covers
native assertion/expectation failures; `compile_failure`, `runtime_failure`,
`timeout`, `infrastructure_failure`, and `invalid_invocation` remain explicit
classifications. Unsupported capability is `UNKNOWN` and has a dedicated
exit code. Expected compile rejection and expected runtime failure are
passing test outcomes, with their evidence preserved.

## Reproducibility

Each process request and stdout/stderr stream is persisted under the artifact
root with a SHA-256 digest. The result binds source, manifest, compiler, and
library roots to a deterministic `run_id`, records the profile and budgets,
and supplies a safe non-executing replay record. Host environment details are
limited to facts needed to distinguish the runner and are not used to alter a
native verdict.

## Boundary review

The repository does not modify Forge. The public seam is a provider command
that accepts a manifest and emits `mncs.check-result/1`; future Forge can
orchestrate this seam exactly like other independent providers. It should not
reimplement manifest semantics or inspect individual assertion fields to
decide family health.

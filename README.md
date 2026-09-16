# mncs-test

Native testing, verification, and conformance framework for MNCS and the
broader MNCS project family.

`mncs-test` is the first MNCS-native application in the family. In source
Profile 0.17, a `test` declaration is a language-owned declaration kind. The
Rust MNCS compiler/runtime/toolchain is the trusted bootstrap that emits the
structural inventory and backend artifact; native `mncs.test.*` modules own
assertion, suite, runner-policy, property, and snapshot semantics. The
canonical entrypoint is the compiler-owned `mncs test` command and it has no
Python fallback.

The machine-readable lifecycle is recorded in
[`native-userland-status.json`](native-userland-status.json):
`native_canonical`. The Python implementation remains available only as an
explicit compatibility/differential oracle through
[`bin/mncs-test-compat`](bin/mncs-test-compat).

The compatibility adapter still consumes `--verification-plan` and the
legacy `mncs.verification-plan/1` transport contract. The native command
currently consumes exact `--test-identity` values and compiler inventory
directly; typed verification-plan ingestion is a next runtime/library slice.
No stale or incomplete plan is silently widened to the full suite.

## Quick start

Build or obtain the current `mncs` executable, then run the checked-in
self-suite from this repository:

```bash
MNCS=/path/to/mncs-language/target/debug/mncs
MNCS_LIBRARY_PATH="$(pwd)/native:/path/to/mncs-language/library" \
  "$MNCS" test tests/self_suite.mncs --format text

# Transparent native packaging adapter.
MNCS="$MNCS" \
MNCS_LIBRARY_PATH="$(pwd)/native:/path/to/mncs-language/library" \
  ./bin/mncs-test
```

The example test module contains arithmetic and boolean assertions, a real
`mncs.std.task.v1` lifecycle witness, deterministic property replay, a native
snapshot witness, an explicit skip, and a native no-fallback policy check. The
successful run is `6` passed and `1` skipped. Machine consumers should use the
default JSON output or an explicitly requested `--check-result` artifact.

## Native test surface

Runtime tests use the first-class declaration directly:

```mncs
mncs 0.17;
module examples.addition_tests;

use mncs.test.assertions.v1;
use mncs.test.suite.v1;

test addition_is_stable() -> (result: TestResult) {
    return from_assertion(equals_i64(42, 19 +% 23, 1001));
}
```

No `[[tests]]` entry or handwritten suite is needed for ordinary runtime
tests. `mncs test` consumes the compiler-owned declaration inventory directly,
then selects and executes those declarations through one retained native
session. The native command intentionally accepts a source and typed selection
options; legacy TOML manifest handling remains in the explicit compatibility
adapter.

The native modules are:

- `mncs.test.assertions.v1`: typed assertions, verdicts, failure kinds, and
  structured expected/actual values;
- `mncs.test.suite.v1`: deterministic bounded aggregation with category
  counters;
- `mncs.test.generative.v1`: pure deterministic bounded generation, replay,
  and a shrink direction;
- `mncs.test.snapshot.v1`: native bounded witness/checksum comparison.

## Commands

```text
mncs test SOURCE [--library ROOT ...] [--filter SELECTOR ...]
          [--test-identity ID ...] [--step-budget N]
          [--result FILE] [--check-result FILE] [--artifacts DIR]
          [--format json|text]

bin/mncs-test-compat discover|validate-manifest|run|run-check|replay ...
```

The native command does not silently discover manifests, launch subprocesses,
or fall back to Python. A legacy manifest or subcommand passed to
`bin/mncs-test` fails closed and points to the explicit compatibility adapter.
The compatibility adapter retains controlled manifest discovery for migration
and differential testing.

The compiler exposes the same provider input directly:

```text
mncs test-inventory path/to/module.mncs
mncs compile path/to/module.mncs --exclude-tests
mncs compile path/to/module.mncs --include-tests --target research-bytecode
```

Production compilation excludes test declarations by default. `--include-tests`
is the explicit test-artifact policy used by `mncs-test`; `--exclude-tests`
states the production policy explicitly and is useful in build scripts.

`replay` is intentionally non-executing. It prints the recorded reproduction
command and run identity so a developer can choose to run it deliberately.

## Result semantics

The semantic protocol is `mncs.test-result/1`; the action-facing adapter is a
versioned `mncs.check-result/1` wrapper. The top-level verdicts are:

| Verdict | Meaning | Default exit |
| --- | --- | ---: |
| `PASS` | all declared native/compile expectations passed | `0` |
| `FAIL` | a native assertion or expected behavior failed | `1` |
| `UNKNOWN` | a declared capability is unsupported | `6` |

The result also carries a `classification` (`test_failure`,
`infrastructure_failure`, `compile_failure`, `runtime_failure`, `timeout`,
`unsupported`, or `invalid_invocation`) and a nested `failure_class` where
needed. A runtime trap expected by a `runtime-failure` entry is a passing
expected-failure test; an unexpected trap is a runtime failure. A compile-fail
entry passes only when the compiler rejects the source and its structured
diagnostic codes match the manifest.

The result's `selection` object is the compact reasoning interface. It records
the selected level, exact selected/available counts, affected-surface count,
structural risks, escalation reasons, plan identity, and graph identity. The
action-facing check preserves a digest-bound result reference and this summary;
detailed per-test output remains in the retained result artifact instead of
being nested into every check or receipt.

For the first-class path, each returned `TestResult` is the native oracle
evaluation for one compiler-inventoried test. The adapter carries those typed
values through `mncs.test.suite.v1::empty` and `observe` in the same retained
session; it does not fold assertions or recompute a native verdict. The
result's `summary.authority` is `native_suite` for this path. Legacy adapter
projections remain only for compatibility manifests and external failures.

Every first-class test preserves declaration, test-case, function, subject,
execution, observation, and oracle-evaluation identities, plus the compiler
source span, raw request, compiler execution status, and artifact digests. The
run records source, manifest, compiler, library-root, host, and runner
provenance. `run_id` is content-derived from the inputs and test selection; it
is not a wall-clock identifier.

## RFC 0034 mapping

The result's `experiment` projection keeps the RFC 0034 distinctions visible:

```text
compiler `test` declaration → TestCase definition
                         → retained-session TestExecution
                         → Observation
                         → native OracleEvaluation
                         → bounded EmpiricalClaim/result projection
```

The production subject identity is separate from the test declaration and
body-sensitive test-case identity. Editing a test creates a new case and run
identity while the verification-only production subject fingerprint stays
stable. A finite PASS is bounded evidence, never a universal proof.

## Actions and Forge boundary

`mncs-actions` provides a composite `actions/mncs-test` action. It invokes this
CLI, packages the `mncs.check-result/1`, execution receipt, evidence manifest,
and raw artifacts, and exposes the verdict, failure class, result path, and
manifest digest. For selected cross-repository proof, Actions invokes
`run-check`; that command resolves a repository-owned `mncs-test` check to its
bounded compiler inventory identities and returns the exact TestResult,
CheckResult, execution identity, inventory identity, and family bindings.
Forge orchestrates this provider boundary without learning test semantics.

The intended future shape is conceptually:

```text
Forge plan → Actions selected proof → mncs-test run-check
      → exact TestResult/CheckResult → reusable receipt → Forge
```

## Trust and bootstrap status

This campaign reaches `native_canonical` for the mncs-test application path:

```text
Stage 0  existing Rust/Python/bootstrap witnesses
Stage 1  host harness exercises the MNCS compiler/runtime
Stage 2  mncs-test launches MNCS-native tests
Stage 3  mncs-test executes its own native suite
Stage 4  native canonical path; host implementation retained only as oracle
Stage 5  future: remove compatibility implementation after corpus parity
```

The normal first-class runtime path uses one compiler invocation, one backend
artifact, one retained `mncs-embed` session, one test batch, and native suite
fold calls over that same session. If the native compiler/runtime is missing,
the command fails closed. Python remains a file/TOML/process/ctypes
compatibility boundary and independent oracle; it is not a canonical runner,
assertion engine, or verdict engine.
Independent Rust/Python tests remain valid where they are differential
witnesses or platform-specific action checks.

## Limitations and pressures

Current limitations are recorded in [`pressures/`](pressures/README.md) and
linked to existing Commons records. The important remaining boundaries are
native manifest/verification-plan ingestion, platform process supervision for
tests that themselves need child processes, and compiler-owned typed inventory
consumption by future MNCS modules. Compiler-owned inventory removes source
regex discovery, and retained embed sessions remove subprocess-per-test from
the native path. Each compatibility workaround is explicit, narrow, named,
and reproducible.
The survey and disposition of pre-existing host-language tests is in
[`docs/external-tests.md`](docs/external-tests.md).

## Development

```bash
python3 -m py_compile tools/mncs_test.py
python3 -m unittest discover -s tests -p 'test_*.py'
MNCS=/path/to/mncs-language/target/debug/mncs
MNCS_LIBRARY_PATH="$(pwd)/native:/path/to/mncs-language/library" \
  "$MNCS" test tests/self_suite.mncs --format text

# Explicit compatibility/oracle validation only.
./bin/mncs-test-compat run --mncs "$MNCS" --library /path/to/mncs-language/library --format text
```

Keep native semantics in MNCS and update the local/Commons pressure record
when a missing capability is encountered. Generated result and artifact
directories under `.mncs/` are run products and should not be committed.

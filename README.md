# mncs-test

Native testing, verification, and conformance framework for MNCS and the
broader MNCS project family.

`mncs-test` is the canonical provider for MNCS source tests. In source Profile
0.17, a `test` declaration is a language-owned declaration kind. The compiler
emits its structural inventory; this runner selects and executes that
inventory, while the native `mncs.test.*` modules continue to own assertion,
suite, property, and snapshot semantics. The launcher only supplies compiler
and platform transport and preserves evidence needed to reproduce a run.

When compiler impact evidence is available, the normal selective entrypoint is
`--verification-plan`. The `mncs.verification-plan/1` document is bound to the
current source bytes and compiler inventory and names exact test-case
identities. Its transport schema, identity algorithm, vocabulary, and
validator are owned by the sibling `MNCS-Commons/src/mncs_commons/verification_plan.py`;
the former local schema copy is removed. mncs-test adds only its inventory
join and executable-test checks. A stale or incomplete plan fails closed; it
never silently falls back to the full suite.

## Quick start

Build or obtain the current `mncs` executable, then run the checked-in
self-suite from this repository:

```bash
MNCS=/path/to/mncs-language/target/debug/mncs
./bin/mncs-test discover --format text
./bin/mncs-test run \
  --mncs "$MNCS" \
  --library /path/to/mncs-language/library \
  --format text
```

The example test module contains arithmetic and boolean assertions, a real
`mncs.std.task.v1` lifecycle witness, deterministic property replay, a native
snapshot witness, and an explicit skip. The successful run is `5` passed and
`1` skipped. Machine consumers should use the default JSON output or the
generated `.mncs/mncs-test-check.json` (`mncs.check-result/1`).

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
tests. `mncs-test` asks `mncs test-inventory` for the compiler-owned
declaration list, then selects and executes those declarations. A manifest
still names the source/module and may set libraries, budgets, filters, and
artifact policy. Explicit entries remain a compatibility form for legacy
function tests and for external compiler-input experiments.

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
mncs-test discover [--root DIR] [--recursive] [--inventory] [--mncs BIN]
                    [--library DIR ...] [--format json|text]
mncs-test validate-manifest [--manifest FILE]
mncs-test run [--manifest FILE] [--mncs BIN] [--library DIR ...]
                 [--embed-library FILE] [--filter TEXT ...]
                 [--verification-plan FILE]
                 [--result FILE] [--check-result FILE] [--artifacts DIR]
                 [--format json|text]
mncs-test replay --result FILE [--format json|text]
```

Discovery is controlled and deterministic. Without `--recursive`, the runner
reads the root `mncs-test.toml` and direct `tests/*.toml` manifests. Recursive
manifest discovery is opt-in; it is not the source-test discovery mechanism.
`discover --inventory` asks the compiler for each runtime inventory. Ordering,
names, source spans, and semantic identities then come from the compiler, not
from Python filesystem scans or source-text matching.

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

## Actions and future Forge boundary

`mncs-actions` provides a composite `actions/mncs-test` action. It invokes this
CLI, packages the `mncs.check-result/1`, execution receipt, evidence manifest,
and raw artifacts, and exposes the verdict, failure class, result path, and
manifest digest. A future family workflow can compose it with the existing
aggregate action; Forge should eventually orchestrate that provider boundary
without learning test semantics.

The intended future shape is conceptually:

```text
forge test → mncs-test run → mncs.check-result/1 → action/Forge aggregation
```

No Forge source is modified by this repository.

## Trust and bootstrap status

This campaign reaches Stage 3 for the native semantic core:

```text
Stage 0  existing Rust/Python/bootstrap witnesses
Stage 1  host harness exercises the MNCS compiler/runtime
Stage 2  mncs-test launches MNCS-native tests
Stage 3  mncs-test executes its own native suite
Stage 4  future: host tests retained only as independent oracles
```

The normal first-class runtime path uses one compiler invocation, one backend
artifact, one retained `mncs-embed` session, one test batch, and native suite
fold calls over that same session. If the shared library is unavailable, the
runner records an explicit subprocess-per-test fallback rather than silently
changing semantic ownership. Python remains a file/TOML/process/ctypes
transport boundary; it carries native values but is not an assertion or
verdict engine.
Independent Rust/Python tests remain valid where they are differential
witnesses or platform-specific action checks.

## Limitations and pressures

Current limitations are recorded in [`pressures/`](pressures/README.md) and
linked to existing Commons records. The important remaining boundaries are
platform process supervision, external structured-result transport, and
advanced callable-value features. Compiler-owned inventory removes source
regex discovery, and retained embed sessions remove subprocess-per-test from
the normal path. Each workaround is narrow, named, and reproducible.
The survey and disposition of pre-existing host-language tests is in
[`docs/external-tests.md`](docs/external-tests.md).

## Development

```bash
python3 -m py_compile tools/mncs_test.py
python3 -m unittest discover -s tests -p 'test_*.py'
MNCS=/path/to/mncs-language/target/debug/mncs
MNCS_LIBRARY_PATH="$(pwd)/native:/path/to/mncs-language/library" \
  ./bin/mncs-test run --mncs "$MNCS" --library /path/to/mncs-language/library
```

Keep native semantics in MNCS and update the local/Commons pressure record
when a missing capability is encountered. Generated result and artifact
directories under `.mncs/` are run products and should not be committed.

# mncs-test

Native testing, verification, and conformance framework for MNCS and the
broader MNCS project family.

`mncs-test` puts the meaning of a test in MNCS. A test is an ordinary MNCS
function that returns a typed `TestResult`; a suite is an ordinary MNCS
function that folds those results into a typed `SuiteSummary`. The external
launcher only supplies the current compiler/runtime, carries bounded request
and result documents, and preserves evidence needed to reproduce a run.

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

The example suite contains arithmetic and boolean assertions, a real
`mncs.std.task.v1` lifecycle witness, deterministic property replay, a native
snapshot witness, and an explicit skip. The successful run is `5` passed and
`1` skipped. Machine consumers should use the default JSON output or the
generated `.mncs/mncs-test-check.json` (`mncs.check-result/1`).

## Native test surface

The current language profile does not yet have a first-class `test`
declaration or function values. The idiomatic surface for this release is
therefore explicit and inspectable:

```mncs
use mncs.test.assertions.v1;

fn test_addition() -> (result: TestResult) {
    return from_assertion(equals_i64(42, 19 +% 23, 1001));
}
```

A suite imports `mncs.test.suite.v1` and calls `summarize<N>` over a static
sequence of result values. The manifest gives each entrypoint a stable test
identity, kind, tag set, optional bounded arguments, and timeout/step budget.
This explicit registry is temporary pressure evidence, not a hidden host-side
test collector.

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
mncs-test discover [--root DIR] [--recursive] [--format json|text]
mncs-test validate-manifest [--manifest FILE]
mncs-test run [--manifest FILE] [--mncs BIN] [--library DIR ...]
                 [--result FILE] [--check-result FILE] [--artifacts DIR]
                 [--format json|text]
mncs-test replay --result FILE [--format json|text]
```

Discovery is controlled and deterministic. Without `--recursive`, the runner
reads the root `mncs-test.toml` and direct `tests/*.toml` manifests. Recursive
discovery is opt-in. A manifest names the MNCS source/module, optional suite,
profile, library roots, and ordered test entries; it does not contain an
assertion language of its own.

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

Native suite summaries are authoritative. The adapter validates their shape
and checks the declared total against the manifest, but it never computes a
replacement verdict by folding individual results. Compile-only manifests
without a suite necessarily use the adapter projection and say so in
`summary.authority`.

Every test preserves its source entry location, raw request, captured
stdout/stderr, compiler execution status, program/function identities when
available, and artifact digests. The run records source, manifest, compiler,
library-root, host, and runner provenance. `run_id` is content-derived from
the inputs and test selection; it is not a wall-clock identifier.

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

The remaining critical-path boundary is the external `mncs execute` process.
The Python adapter is not a second assertion engine; it is a temporary
process/file/transport boundary. Independent Rust/Python tests remain valid
where they are differential witnesses or platform-specific action checks.

## Limitations and pressures

Current limitations are recorded in [`pressures/`](pressures/README.md) and
linked to existing Commons records. The important open pressures are the lack
of process supervision and structured compiler diagnostic APIs, the lack of
in-process invocation, the lack of first-class test declarations/function
values, controlled filesystem enumeration, and string/JSON values at the
MNCS process boundary. Each workaround is narrow, named, and reproducible.
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

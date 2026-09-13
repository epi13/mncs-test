# Writing native tests

Use the current MNCS profile and import the framework records from the
repository's library root.

```mncs
mncs 0.16;
module examples.addition_tests;

use mncs.test.assertions.v1;

fn addition_is_stable() -> (result: TestResult) {
    return from_assertion(equals_i64(12, 5 +% 7, 4101));
}
```

Add the function to a manifest. If a suite is useful, keep the aggregation in
MNCS too:

```mncs
use mncs.test.suite.v1;

fn suite() -> (result: SuiteSummary) {
    return summarize<1>([addition_is_stable()]);
}
```

The manifest entrypoint gives the function a durable identity:

```toml
schema_version = "mncs.test-manifest/1"
name = "addition"
source = "addition_tests.mncs"
module = "examples.addition_tests"
suite = "suite"
profile = "0.16"
libraries = ["native"]

[[tests]]
id = "addition-is-stable"
entry = "addition_is_stable"
kind = "unit"
tags = ["arithmetic"]
```

Use `equals_i64`, `equals_bool`, and bounded byte witnesses for the currently
available value vocabulary. A failing assertion carries expected, actual, and
assertion code in the native record; the adapter adds source location and
captured runtime provenance.

For compile conformance, use `kind = "compile-pass"`, `"compile-fail"`, or
`"diagnostic"`. Compile-fail entries can list diagnostic code prefixes:

```toml
[[tests]]
id = "rejects-invalid-return"
entry = "invalid_return"
kind = "compile-fail"
diagnostic_codes = ["MNE"]
```

For an expected runtime trap, use `kind = "runtime-failure"` and
`expected_status = "runtime_failure"`. This documents intent and prevents an
unexpected trap from being mistaken for a passing test.

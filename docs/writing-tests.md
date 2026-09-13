# Writing native tests

Use Profile 0.17 and import the framework records from the repository's
library root. The declaration itself is the test registration; do not add an
ordinary `fn test_*`, a suite aggregation, or one manifest entry per runtime
test.

```mncs
mncs 0.17;
module examples.addition_tests;

use mncs.test.assertions.v1;

test addition_is_stable() -> (result: TestResult) {
    return from_assertion(equals_i64(12, 5 +% 7, 4101));
}
```

The minimal manifest names the source/module and policy only:

```toml
schema_version = "mncs.test-manifest/1"
name = "addition"
source = "addition_tests.mncs"
module = "examples.addition_tests"
profile = "0.17"
libraries = ["native"]
```

Run and filter through the canonical provider:

```bash
mncs-test discover --inventory --mncs /path/to/mncs
mncs-test run --manifest mncs-test.toml --mncs /path/to/mncs \
  --filter addition
```

The compiler inventory supplies the stable declaration/test-case identities,
source span, signature, effects, capabilities, and production subject
identity. `mncs-test` adds selection, execution, result formatting, and
evidence transport. Use `equals_i64`, `equals_bool`, and bounded byte
witnesses for the currently available value vocabulary. A failing assertion
carries expected, actual, and assertion code in the native record; the
adapter adds source location and captured runtime provenance.

Test declarations obey ordinary effect and capability closure rules. Being a
test grants no ambient authority. Imported-module tests are not implicitly
selected by a root inventory; make each test-bearing module an explicit
runner target when that policy is desired.

For compile conformance, use `kind = "compile-pass"`, `"compile-fail"`, or
`"diagnostic"`. Compile-fail entries can list diagnostic code prefixes:

```toml
[[tests]]
id = "rejects-invalid-return"
entry = "invalid_return"
kind = "compile-fail"
diagnostic_codes = ["MNE"]
```

Compile-pass, compile-fail, diagnostic, malformed-source, and profile-refusal
cases remain external manifest-addressable experiments because their source
is intentionally invalid or is not an executable test declaration. They are
still represented as bounded RFC 0034 observations and use structured
diagnostic fields (code, stage, severity, and span), not human-readable prose
matching.

For an expected runtime trap, use `kind = "runtime-failure"` and
`expected_status = "runtime_failure"`. This documents intent and prevents an
unexpected trap from being mistaken for a passing test.

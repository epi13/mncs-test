# mncs-actions integration

`mncs-actions/actions/mncs-test` is the automation boundary for this
repository. A caller supplies a checked-out `mncs-test` executable (or the
transparent `bin/mncs-test` launcher), the current `mncs` binary, the library
roots, and a manifest.

The action:

1. invokes `mncs-test run` with explicit result and artifact paths;
2. preserves the native result and raw request/stdout/stderr artifacts;
3. validates the emitted `mncs.check-result/1` through the existing
   `run-check` contract;
4. writes the standard execution receipt and evidence manifest; and
5. exposes `verdict`, `failure-class`, `test-result-path`, `evidence-path`,
   and the deterministic manifest digest.

The provider exit codes are retained in the receipt. This lets automation
distinguish assertion failure (`1`), invalid invocation (`2`), infrastructure
failure (`3`), compile failure (`4`), timeout (`5`), and unsupported
capability (`6`) without confusing any of them with a passing test result.

An action caller can compose the result with `mncs-actions/actions/aggregate`.
Future Forge integration should invoke this provider and consume its result;
it should not parse MNCS test functions or duplicate the native taxonomy.

Forge is intentionally not modified by this campaign.

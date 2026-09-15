# mncs-actions integration

`mncs-actions/actions/mncs-test` is the automation boundary for this
repository. A caller supplies a checked-out `mncs-test` executable (or the
transparent `bin/mncs-test` launcher), the current `mncs` binary, the library
roots, and a manifest.

The ordinary action:

1. invokes `mncs-test run` with explicit result and artifact paths;
2. preserves the native result and raw request/stdout/stderr artifacts;
3. validates the emitted `mncs.check-result/1`;
4. writes the standard execution receipt and evidence manifest; and
5. exposes `verdict`, `failure-class`, `test-result-path`, `evidence-path`,
   and the deterministic manifest digest.

The provider exit codes are retained in the receipt. This lets automation
distinguish assertion failure (`1`), invalid invocation (`2`), infrastructure
failure (`3`), compile failure (`4`), timeout (`5`), and unsupported
capability (`6`) without confusing any of them with a passing test result.

An action caller can compose the result with `mncs-actions/actions/aggregate`.
Forge invokes this provider when a verification plan routes to selected
repositories and consumes its result;
it should not parse MNCS test functions or duplicate the native taxonomy.

## Selected family checks

Actions also invokes the repository-owned `run-check` provider interface for a
graph edge whose trusted runner is `mncs-test`. The request binds the check,
contract revision, verification-plan identity, family graph identity, edge
fingerprint, and source-change digest. The provider resolves the exact
`test_identities` and compiler `inventory_identity` in the check manifest,
then delegates execution to the existing `run` path. It does not accept shell
commands or arbitrary executable data from the graph.

The response carries both `mncs.test-result/1` and an exact-check
`mncs.check-result/1`, plus the run identity, runner version, inventory and
check-definition identities, and the family bindings. Actions verifies all of
those bindings before creating the selected-consumer receipt. `FAIL` and
`UNKNOWN` preserve the test result lineage and prevent the composite proof
from passing; an unchanged PASS can be reused only when those bindings still
match.

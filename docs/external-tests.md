# Existing external test infrastructure

The campaign surveyed the current family rather than treating every
Rust/Python/shell test as disposable. The classification is:

## A — now represented natively

The representative bounded behavior of `mncs.std.task.v1` is exercised by
`tests/self_suite.mncs` through `candidate_invalid()`. The assertion,
aggregation, deterministic generator, and snapshot witness logic used for
that verification lives in `native/mncs/test` and is executed by this
provider.

## B — retained as independent witnesses

`mncs-language`'s Rust `mncs-conformance` crate remains valuable for
contract-derived corpus generation, backend comparison, and an independently
implemented reference execution path. It is an oracle/bootstrap witness, not
the implementation of `mncs-test` semantics. Keeping it makes disagreement
scientifically useful instead of making the native framework the only witness
of its own behavior.

## C — exists because the current MNCS boundary is incomplete

The external process/session launcher, compiler diagnostic transport, broad
filesystem discovery, first-class callable/test declarations, and
string/JSON result projection are recorded as `MNCS-TEST-P-001` through
`MNCS-TEST-P-006`. The Python runner and the action shell are limited to those
named boundaries; they do not contain a shadow assertion engine.

## D — correctly remains infrastructure-specific

GitHub Actions composition, cargo toolchain setup, artifact upload, execution
receipts, and the existing family check/evidence packaging are platform
automation. They belong to `mncs-actions` and Commons transport rather than
to MNCS test semantics. Their result contract is intentionally generic so
future Forge orchestration can consume it without learning test internals.

This split is part of the trust model: removing an independent oracle merely
because native tests exist would reduce assurance, while leaving a host
implementation crutch unnamed would conceal language pressure.

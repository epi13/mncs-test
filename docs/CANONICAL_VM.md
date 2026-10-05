# Explicit canonical VM lane

`bin/mncs-test vm` / `canonical-vm-tests/1` composes an exact selected compiler
producer and VM runtime. The existing `mncs test` entry remains the independent
Stage-0 reference lane. The VM transport is an explicit packaging adapter; native
MNCS TestResult and suite-fold policy remain authoritative.

```sh
bin/mncs-test vm --manifest mncs-test.toml --library /selected/stdlib/library \
  --compiler-checkout /selected/mncs-compiler \
  --compiler-executable /selected/mncs-compiler/.bootstrap/target/release/mncs-compiler-stage0-probe \
  --vm-checkout /selected/mncs-vm --vm-executable /selected/mncs-vm/target/debug/mncs-vm \
  --vm-cache /explicit/shared/artifact/cache
```

Environment supplies those exact executable/checkouts through provider-owned
invocation descriptors. No unavailable or stale VM composition falls back to
Language/research bytecode. This lane supports compiler-owned first-class test
inventory and its existing selection/folding contracts; legacy explicit suites,
host grants and unsupported compilation refuse. Use an explicit reference or
target lane for those workloads.

The producer seals first-class callable/signature/declaration/test-case identities
in the artifact. VM validates Test references before dispatch. Structured results
retain compiler producer/build receipt, frozen artifact, exact runtime bytes,
request budget, measured record and execution provenance. Test transport does not
turn VM execution completion into an assertion PASS.

The real self-suite yields six passes and one declared skip on VM and the
independent selected Language reference. Native returned values and statuses
agree. The older compatibility envelope represents a skipped row as
`status: skipped, verdict: PASS`, with native `SKIP` retained; Language's direct
runner exposes row `SKIP`. This pre-existing envelope distinction is not a
semantic disagreement, and the skipped row is not counted as passed.

Research artifact generation remains only on explicitly selected reference,
compatibility or target-specific paths. `migrate.rs` is not used. See
`tests/test_vm_transport.py` and the Environment family proof for identity
refusals, bounded calls and retained execution.

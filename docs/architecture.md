# mncs-test architecture

## Layering

```text
MNCS `test` declaration (Profile 0.17)
        ↓ AST/model identity + compiler test inventory
mncs-test selects TestCase identities
        ↓ one compiled artifact + retained Session batch
TestExecution → Observation → native OracleEvaluation
        ↓ mncs.test-result/1 + RFC 0034 projection
mncs-actions transports mncs.check-result/1
```

The native layer owns assertion meaning, failure categories, deterministic
aggregation, bounded generation, replay inputs, and snapshot witnesses. It is
deliberately made from ordinary MNCS enums, records, functions, sequences,
bounded iteration, and arithmetic. There is no hidden registration table in
Python and no host-side assertion callback.

The adapter owns four platform boundaries that are not test semantics:

1. TOML and controlled manifest/file discovery;
2. starting the `mncs` executable, locating `mncs-embed`, and enforcing an OS
   process timeout;
3. JSON/TOML transport to the current CLI and projection into the family
   `check-result/1` contract; and
4. byte-for-byte capture, hashing, and publication of raw artifacts.

The adapter receives a native result, validates its shape, and carries it
forward. A first-class test's returned verdict is the native oracle evaluation;
the adapter carries those values through `mncs.test.suite.v1::empty` and
`observe` in the retained session, so the native `SuiteSummary` remains the
summary authority. Legacy adapter projections remain only for compatibility
manifests and external failures.

## Manifest and discovery

The root `mncs-test.toml` is the default explicit suite. Direct
`tests/*.toml` files are also discoverable. Recursive filesystem search is
opt-in (`discover --recursive`) and is not used by `run` implicitly. This
keeps discovery deterministic and inspectable while the language/package
system develops a stronger native module discovery contract.

Each normal runtime manifest names one MNCS source/module and policy. It does
not register individual first-class tests. The compiler's inventory is
authoritative for names, ordering, source spans, signatures, effects,
capabilities, and identities. A manifest may still name a suite or explicit
entries during the compatibility window, and external compile-pass,
compile-fail, diagnostic, profile-refusal, and malformed-source experiments
remain explicit because their input is not an executable first-class test.

The compiler inventory is module-scoped by policy: imported-module tests are
not silently included in the root module's inventory. A package tool can
enumerate its explicit module targets without asking Python to recursively
scan source text.

## Execution sequence

For a first-class runtime manifest, the adapter asks `mncs test-inventory`,
compiles once with `--include-tests`, opens one verified `mncs-embed` Session,
and sends the selected calls through `mncs_session_call_batch`. Each returned
value remains a distinct execution observation. The adapter then calls native
`mncs.test.suite.v1::empty`/`observe` through that same session to obtain the
semantic summary. If the embed library is not available, the result records an
explicit subprocess-per-test fallback. A legacy manifest with a suite retains
its compatibility behavior and native suite authority.

Compile-only entries invoke `mncs validate`. A compile-fail or diagnostic
entry is passing only when the compiler rejects the source and every declared
diagnostic prefix is present. This is a temporary structured-output adapter,
not string matching against human compiler prose.

The result has two layers:

- `mncs.test-result/1` carries detailed test outcomes, native results,
  classifications, diagnostics, declaration/test-case/subject/execution/
  observation identities, artifacts, and reproduction;
- `mncs.check-result/1` is a small action/Forge-facing envelope with the
  top-level verdict, claim, digest, unresolved list, compact `selection`
  summary, and a digest-bound reference to the detailed result. Detailed test
  records are not nested into the check, so Actions and Forge can reason from
  a small machine-readable witness and retrieve the raw result only when
  needed.

## Minimum-sufficient verification

`mncs.verification-plan/1` is the selective provider boundary. Ravel supplies a
plan from the compiler's `mncs.semantic-impact/1` projection and inventory;
the runner verifies the current source and subject binding, then selects exact
test-case identities. The plan records its level, impact counts, risks,
escalation reasons, graph identity, and proof stop condition. Plan selection is
identity-based rather than substring-based. A stale or incomplete plan is an
explicit UNKNOWN/invalid invocation condition, never a reason to broaden to a
full suite without a new plan.

## Failure taxonomy

`PASS`, `FAIL`, and `UNKNOWN` are intentionally not collapsed. `FAIL` covers
native assertion/expectation failures; `compile_failure`, `runtime_failure`,
`timeout`, `infrastructure_failure`, and `invalid_invocation` remain explicit
classifications. Unsupported capability is `UNKNOWN` and has a dedicated
exit code. Expected compile rejection and expected runtime failure are
passing test outcomes, with their evidence preserved. Finite passing evidence
is never promoted to universal proof.

## RFC 0034 identity mapping

The first-class path keeps the RFC 0034 epistemic layers visible:

```text
compiler `test` declaration → TestCase definition
                         → retained-session TestExecution
                         → Observation
                         → native OracleEvaluation
                         → bounded empirical result projection
```

The declaration identity names the source slot, while the body-sensitive
test-case identity names the executable case. The production subject identity
and fingerprint are carried separately. A test edit therefore changes test
and experiment evidence without silently changing the production subject.

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

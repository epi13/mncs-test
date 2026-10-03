# mncs-test architecture

## Layering

```text
MNCS `test` declaration (current Profile 0.18; introduced in 0.17)
        ↓ AST/model identity + compiler test inventory
Rust toolchain selects compiler-inventoried TestCase identities
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

The native command owns compiler/toolchain transport and four platform
boundaries that are not test semantics:

1. source loading and explicit library resolution;
2. compiler admission, backend artifact verification, and the retained
   `mncs-embed` session;
3. JSON projection into the family `check-result/1` contract; and
4. bounded artifact publication.

The adapter receives a native result, validates its shape, and carries it
forward. A first-class test's returned verdict is the native oracle evaluation;
the adapter carries those values through `mncs.test.suite::empty` and
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

For a first-class source, `mncs test` obtains the generic
`mncs.declaration-inventory/1` from the compiler front end and projects its
test callables into the retained `mncs.test-inventory/1` runner envelope. It
then compiles once with tests included, opens one verified
`mncs-embed` Session, resolves each selected compiler TestCase identity against
that artifact, and batches the resulting artifact-bound callable references
through the same retained session. Typed values, generic arguments, effects,
and capabilities use the canonical runtime checks; each result carries the
invoked callable, signature, and artifact receipt. The native provider checks
those receipts against compiler-owned inventory data, with no generated
per-test executable dispatch. The toolchain then calls native
`mncs.test.suite::empty`/`observe` through the same session to obtain the semantic
summary. If the native toolchain is not available, the command fails closed.
A legacy manifest retains its behavior only through `mncs-test-compat`.

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

The transport authority is MNCS-Commons, not this runner: the family validator
owns schema revision, identity, vocabularies, proof boundaries, and
cross-repository bindings. mncs-test contributes only role-specific inventory
membership and executable-test checks. The shared mutation corpus is exercised
by family contract tests so malformed identities, stale source bindings,
unknown levels/reasons, incomplete impact, and inventory mismatches have one
disposition across consumers.

## Ambient verification coherence

`mncs.test.verification_coherence` is the native reuse/selection policy
for ambient verification. The host (mncs-environment) measures the
current world — declaration, subject-content, executor, toolchain,
inventory, repository revision, and dirty-content identities — and
transports records; the module owns every decision: strict bound-identity
admission, lifecycle/executor/provider gating, exact pattern resolution
against recorded inventory, a bounded run queue with deferral, and
fail-closed contradictory/unresolved statuses. Fresh obligations queue
for discovery execution; recorded FAIL stays current knowledge.

The module is invocable as the `test-verification-coherence` native
application (`bin/mncs-test-coherence`, contract
`mncs.test-verification-coherence/1`) and as the `evaluate_verification_coherence`
transport in `tools/mncs_test.py`. `bin/mncs-test-provider` likewise
exposes the native batch provider. All three adapters are transport
only; no selection or reuse policy lives in shell or Python.

## Family verification

`bin/mncs-test-verify` (`tools/mncs_test_verify.py`) evaluates one
repository's verification obligations end to end:

```text
obligation inventory
        ↓ host measures (git, content digests, compiler test-inventory; no execution)
native coherence policy → current / queued / deferred / excluded
        ↓ queued native suites, identity-bound
`mncs test` → file-captured mncs.test-result/1 (stdout is never parsed)
        ↓ native digest policy
compact digest + content-addressed receipt → repo-local Store vault
        ↓
one agent-facing report (considered / reused / executed / failed)
```

Soundness rules, never violated to look fast:

- the native coherence module owns every reuse decision; the host only
  measures and transports;
- unmeasurable is UNKNOWN, never queued blindly and never green;
- only `native_first_class_test` obligations with an explicit
  single-source binding execute; everything else is reported with its
  status/reason and left unexecuted;
- a recorded FAIL stays a FAIL (current knowledge);
- a changed test selection always re-executes, even when the bound world
  matches (a `current` verdict carries no resolved selection, so the host
  proves selection equivalence from wildcard/exact patterns only);
- verifier upgrades invalidate (the verifier implementation is digested
  into the repository fingerprint);
- corrupt or missing receipt files heal from vault authority (the
  Store head is re-projected; re-execution happens only when no
  authoritative evidence matches); deterministic execution makes
  concurrent races converge (the Store reports DUPLICATE, receipts stay
  valid);
- PASS→FAIL transitions report `REGRESSION`, FAIL→PASS report `FIXED`;
  history recall across an intervening different verdict reports a
  `recalled` transition and never pretends it executed now.

The repository fingerprint binds the revision, MNCS-source status,
verification manifests, the verifier implementation, outside
library-root content, and the ambient stdlib-root authority
(measured since the v2 campaign closed that hole), so
documentation/script changes preserve reuse while any suite edit
re-executes. Per-obligation file precision needs the compiler to
report a resolved module closure (pressure MNCS-TEST-P-014; contract
requested in `docs/compiler-closure-contract.md`). Exit codes are
`0` pass, `1` fail, `3` incomplete, `2` harness error.

Coherence policy v2 (`mncs.test.verification_coherence_v2`,
request `/2`) adds semantic closure reuse: evidence stays admissible
across revisions with reason `closure_current` when both sides carry
a trusted, equal source closure under the same fileset rule, even
when the revisions differ. The host closure covers every `.mncs`
file under the obligation's library roots plus the repo manifests,
the verifier, and the ambient stdlib authorities; the MNCS-only
fileset is grounded in toolchain behavior (resolution reads
`.mncs` candidates only; test bodies run with zero grants). Policy
v1 stays frozen for the environment ambient flow. `verify --changed`
is the affected-test dry run: same measurement and native
coherence, no execution or writes, reporting `would-reuse` /
`would-execute` with the moved identities named per obligation.

Evidence authority lives in the Store vault, not the receipt files
(pressures MNCS-TEST-P-015/P-016, both closed). Each admission is an
immutable vault object under an obligation-scoped identity carrying
core + lineage + producer; the head is the lineage-chain tip, with a
deterministic smallest-id rule for concurrent forks. Receipt files
are projections repaired Store→file when divergent; unreadable
authority fails closed (fresh execution, never silent reuse); only
`--no-store` accepts file authority, explicitly degraded. When the
head is stale, the host walks the head-anchored chain for the newest
vaulted evidence matching this exact semantic world and native
policy verifies admission on a second pass; recalled reuse is
reported distinctly (`recalled-closure`, recall note, head context).

Policy generations, steady state: v1 (`/1`) serves the environment's
ambient verification flow and the frozen oracle's compatibility
baseline (both live); v2 (`/2`) is canonical for provider
verification (`mncs-test-verify` direct and via `mncs-env test`).
Result envelopes are identical (`/1`); converging ambient to v2
needs environment-owned closure measurement and stays future work.
Re-audited 2026-10-03: `mncs-environment/mncs_env/verification.py`
still consumes request `/1`, so the split stays deliberate.

Run cost model (measured 2026-10-03 on this repo, 4 obligations):

- one verify run holds at most one read-only and one read-write
  Store handle (`RunStores` in `tools/mncs_test_verify.py`), opened
  lazily and closed explicitly even on failure; open failures are
  cached so an unreadable vault reports `unavailable` per obligation
  without repaying the failing open. Sharing is sound because
  `current_generation` re-reads the head file on every access and
  `find_bound_objects` rebuilds its projection when the head moves.
- each distinct source closure is measured once per run (pure
  function of libraries/repo/manifest/toolchain); `closure_files`
  counts actual hashing work, not obligations times files.
- warm verify (0 suites): ~2.5s, 6 subprocesses
  (2 inventory, 1 coherence, toolchain, 2 git), 1 store open:
  ~1.5s Store session open (Store-owned artifact verification),
  ~0.4s native coherence, ~0.36s compiler inventory, the rest
  measurement. Previously ~7s with 4 store opens.
- cold verify (2 suites, existing vault): ~6.2s, 10 subprocesses,
  2 store opens (1 shared reader, 1 shared writer). A first-ever run
  with no vault opens nothing for reads (`empty` without opening)
  and opens the writer once for admission.
- coherence is already one native batch per pass (initial plus, only
  when stale rows have recall candidates, one recall pass); one
  malformed obligation cannot contaminate the others.
- the remaining warm floor is owned elsewhere: Store session init
  (pressure MNCS-TEST-P-017) and `mncs` process startup per
  inventory/coherence call. No test-owned resident daemon exists:
  it would amortize only the Store open while adding lifecycle and
  staleness risk, so the fix belongs in the Store layer.

Per-test selection stays out: per-test `semantic_fingerprint` is the
canonical form of the test function itself
(`mncs-model/src/identity.rs`), so a callee edit does not move the
caller's fingerprint. Fingerprints are not transitive; obligation
granularity stands until a compiler contract states otherwise
(P-014 notes).

## Native digest

`mncs.test.digest` owns the relevance decision for agents: counts fold
through `mncs.test.suite` (never recomputed), failure rows echo the
oracle expected/actual/code triple plus source location in test order
under a `max_failures` bound, omissions are counted, and verdicts are
never invented. The host projects the documented result schema into a
`DigestRequest` (mechanical field re-keying; unknown vocabulary fails
closed) and renders the returned digest as text. Measured on a two-test
failing suite: 21223-byte result → 1158-byte digest JSON → 322-byte
text. `bin/mncs-test-digest` is the transport adapter and
`tools/mncs_test_digest.py` the transport; both are native-first and do
not import the frozen compatibility oracle.

## Capability surface and environment routing

The provider seam is routable through the environment without secret
knowledge:

- `mncs.test-result/1` → `bin/mncs-test` (one native suite);
- `mncs.test-verification-coherence/1` → `bin/mncs-test-coherence`;
- `mncs.test-obligation-selection/1` → `bin/mncs-test-selection`;
- `mncs.test-digest/1` → `bin/mncs-test-digest`;
- `mncs.test-verify/1` → `bin/mncs-test-verify` (effects `verify`).

From an entered session, `mncs-env test <session> --checkout <repo>`
resolves the checkout, invokes the bound verify capability with that
checkout as its working directory, and streams the provider report; the
invocation is recorded in the session store. Capability selection and
reuse policy live in the native modules; the environment only routes.

## Doctor and Forge posture

Doctor composes through `mncs-doctor verify
--verify-cmd="mncs-test-verify ..."`; provider exit codes propagate
honestly (INCOMPLETE fails verification with the compact reason
attached). Doctor repairs infrastructure, never verdicts: corrupt
receipts heal by re-execution inside the verifier, and Doctor's own
inventory cache refreshes through normal runs. mncs-test deliberately
publishes no competing `:repository-remediation` capability; that
contract belongs to mncs-doctor's provider.

The native `mncs.test-result/1` and `mncs.check-result/1` envelopes
satisfy Forge's provider validators (schema, provider, verdict
vocabulary, per-test id/verdict, run identity). Actions
selective-family-proof already invokes direct-executable runners
natively (legacy `run --manifest` only for `.py` runners); the
remaining legacy bit is Forge's own `mncs_development._test_command`
in the single-repo invoke path, which is Forge-owned work (pressure
MNCS-TEST-P-013).

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

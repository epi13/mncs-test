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
aggregation, bounded generation, replay inputs, snapshot witnesses, and the
family-facing CheckResult policy projection. It is deliberately made from
ordinary MNCS enums, records, functions, sequences, bounded iteration, and
arithmetic. There is no hidden registration table in Python and no host-side
assertion callback.

The host command owns compiler/toolchain transport and the platform
responsibilities that remain outside test semantics:

1. source loading and explicit library resolution;
2. compiler admission, backend artifact verification, and the retained
   `mncs-embed` session;
3. projection of runtime values into bounded application records plus artifact
   paths; and
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

For bounded explicit-manifest test declarations, the host decodes TOML and
projects typed fields into `mncs.test.manifest_test_validation_v1`. MNCS owns
test-id uniqueness, entry/kind policy, tags and argument shape, numeric
constraints, expected statuses, and diagnostic-prefix checks, preserving the
validator's first-error order. The native result returns canonical kind, tag,
and diagnostic-code values plus whether the opaque `arguments` field needs its
empty-list default. Python decodes those values into the Test envelope while
preserving explicit argument trees. The host still owns TOML parsing, source
and library path resolution, and the compatibility validator for declarations
outside the native request bounds (16 tests, 32 tags per test, 256 diagnostic
codes per test, 512 UTF-8 bytes per projected string, and a 65,536-byte
request). The `validate-manifest`, `discover`, and `run` commands use the
native validator when the selected Stage-0 executable is available.

The pre-test policy application validates bounded manifest headers and
`host_grant_sets` before explicit test declarations, preserving the original
first-error order. Python decodes TOML and projects typed values; MNCS owns the
schema version, required name/source/module fields, suite and profile shape,
positive step and timeout budgets, library-array policy, grant-set shape and
identity uniqueness, capability/locator requirements, duplicate pairs, and
byte-array presence, length, and signed byte-range policy. Its result includes
the effective profile, step budget, timeout, and validated library strings,
including native defaults of `0.17`, `100000`, and `60`. Python only decodes
those fields and resolves source/library paths. MNCS also returns normalized
set/grant rows from the validated typed request, applying empty locator/byte-list
defaults; Python only decodes those rows into the established Test envelope.
The host projects a bounded signed integer sequence for MNCS; booleans,
non-integers, and values outside signed 64-bit representation become an
invalid sentinel for the native range check. Native validation is used for at
most 16 libraries, 16 grant sets, 32 grants per set, 16 table keys, 1,024
projected byte values total, and a 65,536-byte request, with the shared
512-byte projected-text bound. Inputs above the projection budget use the
compatibility validator, preserving the manifest's 4,096-value per-grant cap,
raw values, and existing error messages. Arrays longer than 4,096 can be rejected from
their count without copying values. The native request stays within Profile
0.18's 1,024-element sequence capacity.

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

Compile-only entries invoke `mncs validate`. The MNCS application
`mncs.test.compile_verdict_v1` owns the pass, expected-failure,
expectation-mismatch, diagnostic-mismatch, and compiler-failure decisions for
compile-pass, compile-fail, and diagnostic entries. The host adapter carries
the raw process exit code and bounded structured compiler fields, then maps the
native verdict into the established result envelope.

For decoded runtime outcomes, `mncs.test.execution_adjudication_v1` owns the
Test runner's execution-status, expected-failure, native-verdict, and suite
result policy. Retained-session batches are projected into bounded cases and
adjudicated in MNCS batches of at most 256. The subprocess fallback projects
bounded observations (start failure, timeout, structured-result presence,
exit status, and diagnostic presence) to
`mncs.test.process_transport_adjudication_v1`. That application classifies
the process outcome and, for structured results, delegates to
`execution_adjudication_v1` in the same native call. The Python adapter keeps
process creation, typed-value projection, detailed diagnostics, and the public
result envelope; it renders the native decision while retaining raw
diagnostics and process artifacts. If the native classifier itself cannot
launch after the host observed a process-start failure or timeout, the adapter
preserves that direct platform outcome so the unavailable classifier does not
erase the original failure. Native application calls, including the
obligation-selection
CLI, share `tools/mncs_test_native.py` for bounded request transport and
selected-workspace library resolution, so linked worktrees do not depend on
the original repository path depth.

`mncs.test.check_result_projection_v1` owns the family-facing CheckResult
policy: fallback claim/scope text, first failing identity selection, compact
summary text, unresolved evidence, and digest-bound references. The adapter
supplies bounded fields and preserves the opaque `selection` object. Failure
messages cross the 1,024-byte native field bound as UTF-8-safe chunks; the
adapter rejoins those chunks when it writes the public JSON document. The
compact count text reuses `mncs.std.json_emit.v1::put_u64`.
`mncs.test.public_result_json_token_v1` consumes one compact JSON document in
UTF-8-safe chunks and uses `mncs.std.json_cursor.v1` to validate and traverse
its native token stream. The adapter no longer recursively projects Python
values into a typed-value node/edge tree. For bounded values with only built-in
string object keys, the adapter uses a conservative size preflight and sends
unsorted compact JSON to `mncs.test.public_result_json_key_order_v1`. That
application parses the token stream into a bounded native value, compares
decoded UTF-8 keys, and emits compact JSON in sorted key order. It accepts at
most 4,096 input bytes, depth 12, 512 values, 64 object members, 256-byte keys,
and 1,024-byte scalar text. The host verifies the packed source digest before
dispatch. Non-string mapping keys, oversized values, and native unsupported or
budget outcomes retain Python's sorted compact-JSON compatibility path. MNCS
then scans the ordered document, enforces token, depth, member, text, and byte
bounds, and emits the indented public format with a trailing newline. It
returns output in
8,192-byte windows while carrying cursor and SHA-256 state. The aggregate
compact-input and formatted-output limits are each 65,536 bytes; the native
path accepts at most 512 values, depth 63, 64 children per object, 256-byte
keys, and 1,024-byte scalar text. Each continuation repeats the bounded source
document and MNCS recomputes its SHA-256 before accepting the carried cursor
and output-hash state. Python rejoins returned UTF-8-safe pages, writes files,
and independently checks the final output digest. Values outside native
bounds and native budget exhaustion retain the host compatibility serializer;
native transport and runtime errors remain errors. The 33 KB differential
case stays on the native path without per-byte structured-input node growth.

The family-check response records `execution.check_result_sha256` from the
serialized `mncs.check-result/1` document produced by this same public-result
path. The digest matches the public JSON bytes, including ordering,
indentation, and the final newline. Values outside the native bounds or runs
that exhaust the native step budget retain the compatibility serializer.

The successful `mncs.family-check-response/1` envelope is also emitted through
the bounded public-result serializer when its complete JSON tree fits. Its
UTF-8 bytes match the compatibility formatter; over-bound trees and exhausted
native step budgets retain the host formatter. The infrastructure-failure
envelope remains host-formatted so it can still be emitted when native setup
or execution fails.

`mncs.test.result_identity_v1` constructs canonical run-identity JSON and
hashes it with `mncs.std.sha256.v1` for bounded inputs. Python projects typed
fields into the request and formats the final result envelope; out-of-contract
run identities retain the compact-JSON compatibility path. The batch identity
application accepts up to 16 payloads, 64 chunks, 65,536 bytes, and the
toolchain's 8,000,000-step ceiling per request. `ArtifactStore` uses the
streaming identity application when a payload or file exceeds the batch's
step limit but is at most 65,536 bytes. It carries native SHA-256 state between
calls, with at most fourteen 1,024-byte chunks (14,336 bytes) per call, so the
continuation is bounded to five application executions. The maximum per-call
step estimate remains below 8,000,000. After three native
application hashes in one store, it compiles the same MNCS module to a backend
artifact and retains an `mncs-embed` session. That session calls the pure MNCS
identity and SHA-256 functions in bounded windows for payloads and files up to
512 KiB. A differential test covers a 512 KiB payload and a 393,233-byte compiler
artifact against independent Python `hashlib` results. The retained session
adds a one-time compile and open cost; no throughput gain over host `hashlib`
is claimed. `manifest_provenance` sends compiler, source, and manifest files up
to 14,336 bytes through `ArtifactStore`, keeping each small provenance hash
within a single bounded MNCS request. Larger files through 512 KiB use its
bounded file adapter; after the retained session is warm, that adapter hashes
the runner source through the MNCS stream in 14,336-byte windows. Files above
512 KiB retain host fallback. Before retained-session warm-up, the existing
native application handles files through 65,536 bytes; larger files fall back
to the host until warm-up. If embedding fails, bounded files use the existing
native application through 65,536 bytes and larger files use host fallback.
Library directory enumeration and per-file reads stay on the host. For a
directory with at most 128 entries, a request within 65,536 bytes, and at most
1 MiB of leaf bytes, `ArtifactStore` hashes eligible leaf files through
`mncs.test.result_identity_v1`: files through 65,536 bytes use a native
application or the retained session; larger files through 512 KiB use the
retained session after warm-up. Oversized files, a retained session that is
not warm or unavailable, and native failures retain the exact host digest and
report its fallback reason. `mncs.test.library_tree_identity_v1` owns the
final ordered `path NUL leaf-digest LF` fold. The adapter reports leaf and
tree implementations separately and retains an explicit host fold when the
entry, path, or request bounds are exceeded or native execution fails.
Each retained window uses one MNCS function that validates the
request and advances SHA-256 state together; the final window also finalizes the
digest in that same call, with no separate initialization or finalization call
from Python. Larger artifacts encountered
before warm-up, or any artifact over 512 KiB, retain the host streaming
fallback. Python still reads and writes files, supplies bounded typed values,
enumerates library directories, and preserves explicit host fallback for
unavailable or out-of-bound leaf, tree, and source hashes.

`mncs.test.lineage_identity_v1` now constructs and hashes per-test execution
and observation identity JSON from typed index, run, test-case, status, and
verdict fields. It uses the shared `mncs.std.json_emit.v1` quoting and integer
operations with Python-compatible short escapes, then streams bytes through
`mncs.std.sha256.v1` in 64-byte pages. Each request accepts at most 16 rows and
16,384 aggregate input bytes; run, test-case, status, and verdict strings are
bounded to 256, 512, 64, and 32 UTF-8 bytes. The adapter batches within these
limits and retains the prior compact-JSON hash path for other value types or
over-bound fields. Differential tests compare both digests to Python's
independent `json.dumps` and `hashlib` behavior, and the runner self-suite
verifies the integrated lineage values.

`mncs.test.inventory_filter_v2` owns substring filtering over the Test
inventory. The adapter prepares the runner's existing searchable fields and
projects native-selected indices back to their records. Candidate and filter
batches are bounded at 16. Each MNCS request scans a complete search text up to
65536 UTF-8 bytes in at most 17 UTF-8-safe segments and filters up to 1024 bytes.
The host no longer splits long candidate text into overlapping windows. The
MNCS kernel directly scans matches contained in each part and checks only starts
that cross a part boundary, while rejecting endpoint mismatches before a full
comparison. The adapter estimates scan, first-byte hits, and request-decoding
work, splits expensive batches before execution, and caps each selection at 64
native calls.
Search text over 65536 bytes, filters over 1024 bytes, a single pair that still
exceeds the native 2097152-step budget, and plans that exceed the call limit
retain the prior host filter as an explicit, reported fallback.

`mncs.test.exact_identity_selection_v2` owns matching family-check compiler
test identities against the current inventory. The adapter retains sorted
request order, segments UTF-8 identities into at most sixteen 512-byte parts,
adaptively batches native comparisons, and projects selected inventory
indices back to their records. Identities beyond 8192 bytes, work plans beyond
the step budget, and plans beyond 64 native calls use the previous host
selection explicitly and report that authority.

Verification coherence also uses the shared native-application transport, so
its descriptor resolves libraries from the selected Environment closure.
Repository verification resolves declared sibling-library paths from the
checkout's bounded ancestor roots, keeping the same obligations usable in
linked worktrees.

When a result has no native suite fold, the MNCS application
`mncs.test.result_summary_v1` owns its aggregate counters. The adapter sends
bounded verdict, status, native assertion-count, and failure-class fields;
the application folds core totals through `mncs.test.suite` and applies the
result-envelope failure-class projection, including expected failures. The
Python runner validates the returned counters and records
`mncs.test.result_summary` as their authority.

The result has two layers:

- `mncs.test-result/1` carries detailed test outcomes, native results,
  classifications, diagnostics, declaration/test-case/subject/execution/
  observation identities, artifacts, and reproduction;
- `mncs.check-result/1` is a small action/Forge-facing envelope with the
  top-level verdict, claim, digest, unresolved list, `selection` field, and a
  digest-bound reference to the detailed result. Detailed test
  records are not nested into the check, so Actions and Forge can reason from
  a small machine-readable witness and retrieve the raw result only when
  needed. The native check-result projection application assembles this policy
  envelope; the host adapter still serializes it as JSON and publishes its
  artifact.

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
  matches: `mncs.test.verification_selection_v1` resolves exact and
  wildcard declarations against the compiler inventory and decides
  receipt coverage; the host canonicalizes inventory/receipt identity
  order for transport and validates the native result;
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
head is stale, the host walks the head-anchored chain and filters by
producer provenance. The native selection application resolves the
current requirement and admits only historical receipts with exact
coverage. A separate native recall application batches semantic
comparisons through the v2 `closure_admits` predicate, including its
trusted, non-empty closure checks. Native coherence policy then verifies
each selected receipt on a second pass; recalled reuse is reported
distinctly (`recalled-closure`, recall note, head context).

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
- coherence is one native batch per pass (initial plus, when selected
  recall candidates exist, one recall pass). Semantic recall batches
  at most 256 candidate comparisons per process and splits larger
  workloads into bounded calls; one malformed obligation cannot
  contaminate the other coherence rows.
- selection admission is a native bounded pass for current heads and
  stale histories. It batches at most 32 scans and 256 head-plus-candidate
  comparisons per process, with a 128 KiB identity-work budget and a 1 MiB
  request ceiling; large history
  records are split or fail closed from recall.
- the remaining warm floor is owned elsewhere: Store session init
  (pressure MNCS-TEST-P-017) and `mncs` process startup per
  inventory, coherence, and recall call. No test-owned resident daemon exists:
  it would amortize only the Store open while adding lifecycle and
  staleness risk, so the fix belongs in the Store layer.

Per-test selection stays out: per-test `semantic_fingerprint` is the
canonical form of the test function itself
(`mncs-model/src/identity.rs`), so a callee edit does not move the
caller's fingerprint. Fingerprints are not transitive; obligation
granularity stands until a compiler contract states otherwise
(P-014 notes).

## Native digest

`mncs.test.digest` owns the relevance decision and typed projection for
agents: `structured_read` validates the bounded `mncs.test-result/1` view,
including verdict and failure-kind vocabulary; counts fold through
`mncs.test.suite` (never recomputed); failure rows echo the oracle's
expected/actual/code triple plus source location in test order under a
`max_failures` bound; omissions are counted; verdicts are never invented.
The host wraps the original result, aliases the reserved `scope.module`
wire key, fills the result schema's optional source-span location with
zeroes, and renders the native digest as text. Measured on a two-test
failing suite: 21223-byte result → 1158-byte digest JSON → 322-byte text.
`bin/mncs-test-digest` is the transport adapter and
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

## Verification comparison-work admission

`mncs.test.verification_selection_v1::selection_work_app_v1` owns the
conservative comparison-work model used before receipt selection. The model
counts pattern resolution, duplicate checks, required-set sorting, inventory
ordering, and recorded-set comparisons. Its cumulative arithmetic uses
`mncs.std.budget.v1`; limits remain Test policy. Rejection is absorbing and
never authorizes receipt reuse.

The adapter measures UTF-8 byte lengths and aggregate receipt bytes, submits
up to eight bounded scans per native preflight, validates returned costs,
and packs the actual comparison requests within their JSON and process
bounds. It contains no alternative work model. Aggregate receipt lengths
keep the largest preflight within the existing structured-node limit.
The additional preflight process is explicit in verification statistics as
`selection_work_runs`; it is not evidence of reduced total execution cost.

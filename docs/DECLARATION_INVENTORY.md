# Compiler declaration inventory boundary

`mncs declaration-inventory <source>` is the authoritative source for test
declarations consumed by this repository. The runner builds an artifact-bound
callable reference from the compiler-owned test, declaration, callable, and
signature identities, then invokes the selected declaration through
`mncs-embed`'s generic identity dispatcher. Runtime type arguments and typed
values pass through the existing specialization and execution machinery.

The retained compiler session exposes its artifact-bound callable metadata
through `mncs_session_callable_bindings`. The runner transports those
compiler-issued bindings with the TestCase execution receipts; the native
provider checks callable, declaration, signature, TestCase, and owning
artifact identities together. Each result carries a digest of the exact
binding rows used for that batch. No MNCS identity table is generated.

The provider request's bounded byte views are a transport representation of
the compiler metadata. Type identity, record fields, finite variants, exact
integer types, and sequence bounds remain compiler-owned and are materialized
by the generic structured projection boundary.

The latest verification outcome is recorded in the current Commons
observation for `MNCS-LANG-4F28CEEA0AD8`. Local pressure `MNCS-TEST-P-007`
is resolved: `tests/test_provider_projection.py` proves cross-module receipt
validation and visibility of a newly added TestCase without inventory source
generation.

The Profile 0.18 `provider_call` capability remains a separate strict
admitted-provider boundary. Provider descriptor admission still validates
provider, method, interface, revision, capability, and effect metadata; the
callable identity dispatcher selects MNCS declarations within a loaded
artifact and does not admit arbitrary host methods.

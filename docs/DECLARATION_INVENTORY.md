# Compiler declaration inventory boundary

`mncs declaration-inventory <source>` is the authoritative source for test
declarations consumed by this repository. The runner builds an artifact-bound
callable reference from the compiler-owned test, declaration, callable, and
signature identities, then invokes the selected declaration through
`mncs-embed`'s generic identity dispatcher. Runtime type arguments and typed
values pass through the existing specialization and execution machinery.

`tools/generate_provider.py` now projects compiler inventories from the
self-suite and a separate test module into
`native/mncs/test/provider_inventory.mncs`, a data table used by the native
provider to validate invocation receipts across modules. It emits no
executable dispatch branches. Its remaining removal criterion is direct
compiler/runtime projection of this declaration table into the provider
artifact. Test discovery and result aggregation remain owned by MNCS source;
Python handles inventory transport and artifact generation only.

The latest verification outcome is recorded in the current Commons
observation for `MNCS-LANG-4F28CEEA0AD8`; `MNCS-TEST-P-007` now tracks only the
remaining inventory data-projection boundary.

The Profile 0.18 `provider_call` capability remains a separate strict
admitted-provider boundary. Provider descriptor admission still validates
provider, method, interface, revision, capability, and effect metadata; the
callable identity dispatcher selects MNCS declarations within a loaded
artifact and does not admit arbitrary host methods.

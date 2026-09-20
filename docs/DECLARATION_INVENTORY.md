# Compiler declaration inventory boundary

`mncs declaration-inventory <source>` is the authoritative source for test
declarations consumed by this repository. `tools/generate_provider.py` maps
the generic callable entries whose `callable_kind` is `test` into the
provider's compatibility metadata and generated typed binding.

The generated `native/mncs/test/provider_inventory.mncs` remains necessary
for now because MNCS has no identity-bound reflective invocation operation
that accepts an arbitrary callable identity together with heterogeneous typed
arguments. Named typed entrypoints and explicit generic type arguments are
already supported. This is recorded as `MNCS-LANG-4F28CEEA0AD8` in Commons and
`MNCS-TEST-P-007` locally. The generator is intentionally limited to
identity/signature binding; test discovery, assertion semantics, result
aggregation, and provider policy remain native.

The current Profile 0.18 verification is preserved in Commons observation
`MNCS-LANG-4F28CEEA0AD8--OBS-3933B274C547`, bound to the current named-call
conformance and generated-dispatch reproducer. The earlier capability and
compiler-inventory identities remain archived in the superseded Commons
observation record.

The Profile 0.18 `provider_call` capability was checked during this campaign.
It is a generic admitted-provider boundary whose runtime receives one typed
request and an expected result type; the host provider registry still selects
an admitted provider's fixed entry module/function. It therefore does not
provide callable-identity lookup with heterogeneous argument binding and does
not retire the transparent inventory-bound adapter.

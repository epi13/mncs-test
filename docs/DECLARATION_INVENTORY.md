# Compiler declaration inventory boundary

`mncs declaration-inventory <source>` is the authoritative source for test
declarations consumed by this repository. `tools/generate_provider.py` maps
the generic callable entries whose `callable_kind` is `test` into the
provider's compatibility metadata and generated typed binding.

The generated `native/mncs/test/provider_inventory.mncs` remains necessary
for now because MNCS has no reflective invocation operation that accepts an
arbitrary callable identity together with heterogeneous typed arguments.
This is recorded as `MNCS-LANG-5E290D20B90B` in Commons and
`MNCS-TEST-P-007` locally. The generator is intentionally limited to
identity/signature binding; test discovery, assertion semantics, result
aggregation, and provider policy remain native.

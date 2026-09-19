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

The current Profile 0.18 verification is preserved in Commons observation
`MNCS-LANG-5E290D20B90B--OBS-5A452D3FA3A9`, bound to capability identity
`sha256:8dab5b8f673a2d79d27f1f90eebb14500483e9942cd1b06b105458bf96385294`
and compiler inventory identity
`sha256:e610453d5b7581c7da81eb8c1258a4beaeb02d4551e568b7b9684fc69a9d4186`.

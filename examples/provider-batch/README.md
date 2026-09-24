# Provider batch receipts

Provider requests need compiler-issued test, declaration, callable, and
signature identities plus the artifact identity returned by the retained MNCS
session. Those values are produced by the compiler and runtime for each
execution, so static request JSON cannot establish a valid batch.

The maintained end-to-end construction is in
`tests/test_phase6_provider_generation.py`: it queries inventories from two
source modules, executes selected identities through `mncs-test`, and passes
the runtime receipts to the admitted native provider. The provider rejects
missing, stale, or inconsistent receipts.

# Family verification checks

`family-verification-checks-v1.json` is the repository-owned map from a
stable family check identity to a trusted runner.  For the behavioral
`mncs-test:verification-plan-contract` check, the selected test identities are
explicit architectural intent; the `inventory_identity` is generated from the
language compiler's generic `mncs.declaration-inventory/1` result, projected
only into the legacy selector envelope used by this contract.

Refresh the compiler-bound fact after changing the compiler, test source, or
selected test identities:

```bash
python3 tools/generate_family_verification_checks.py \
  --mncs /path/to/mncs \
  --library /path/to/mncs-test/native \
  --library /path/to/mncs-language/library
```

Use `--check` in a repository or family synchronization gate.  The generator
does not infer consumer relationships, execute arbitrary commands, or decide
test verdicts.  Actions still selects the trusted `mncs-test` adapter from the
runner kind, while this repository owns execution semantics and exact test
identity validation.

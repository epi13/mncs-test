# mncs-test execution contract

`mncs-test` is an independent MNCS family consumer. The semantic testing
surface belongs in `native/mncs/test`; the Python and shell files in this
repository are deliberately narrow transport adapters.

Establish the bounded family context and current compiler/capability identity
before broad search. Query Commons for an existing pressure before recording
another one; the open callable-inventory pressure is
`MNCS-LANG-5E290D20B90B` / `MNCS-TEST-P-007`.

Before adding host code, check the current `mncs-language` profile and
standard library. If the missing capability is semantic rather than a
platform boundary, record a pressure under `pressures/` and route its Commons
counterpart instead of hiding the gap behind a helper.

The adapter may:

- parse the TOML manifest and perform controlled file discovery;
- launch the current `mncs` executable with an explicit request;
- enforce an operating-system timeout and capture stdout/stderr;
- carry JSON/TOML across the current external CLI boundary; and
- preserve reproducible raw artifacts and provenance.

It may not generate property cases, evaluate assertions, aggregate native
verdicts, or silently reinterpret a returned MNCS value.

## Local validation

From this repository, with a built compiler in the sibling checkout:

```bash
MNCS=/home/epi13/Documents/Projects/mncs-language/target/debug/mncs
./bin/mncs-test discover --format text
./bin/mncs-test run --mncs "$MNCS" \
  --library /home/epi13/Documents/Projects/mncs-language/library \
  --format text
python3 -m unittest discover -s tests -p 'test_*.py'
```

The `mncs` and library paths are intentionally explicit: the repository must
remain usable as an external consumer rather than depending on privileged
compiler internals.

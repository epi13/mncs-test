# Provider batch receipts

Provider requests need compiler-issued test, declaration, callable, and
signature identities plus the artifact identity returned by the retained MNCS
session. Those values are produced by the compiler and runtime for each
execution, so static request JSON cannot establish a valid batch.

The maintained end-to-end construction is in
`tests/test_provider_projection.py`: it reads artifact-bound callable
metadata from the retained compiler session, executes selected identities
from separate modules, and passes the metadata and runtime receipts through
the bounded provider request contract. A later-added TestCase is visible
without regenerating provider source. The provider rejects missing, stale,
or foreign artifact receipts.

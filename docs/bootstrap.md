# Bootstrap and trust model

`mncs-test` is not claiming that the host boundary has disappeared. The
campaign's current trust chain is:

1. existing Rust/Python tests are Stage 0 bootstrap and independent witnesses;
2. the current `mncs` compiler owns first-class declaration identity and the
   structural test inventory;
3. native MNCS assertion and aggregation modules form the semantic test core;
4. the checked-in `tests/self_suite.mncs` is discovered, compiled once, and
   executed through a retained `mncs-embed` session by
   `mncs-test` itself;
5. future releases can remove host tests from the critical path one capability
   at a time, retaining host implementations only as differential or platform
   oracles.

The meaningful semantic core in this repository is MNCS. Python does not
choose property inputs, evaluate predicates, fold suite results, or translate
an assertion into PASS. It queries the compiler inventory, carries bounded
JSON/TOML or the stable embed C ABI, and preserves evidence. OS process
supervision and external structured transport remain platform boundaries.

The remaining bootstrap boundary is therefore exact and auditable:

```text
mncs-test adapter → compiler inventory → retained mncs-embed Session
                 → native MNCS functions → RFC 0034 result projection
```

The subprocess-per-test path remains only as an explicit fallback when the
embed shared library cannot be loaded. The corresponding language pressures
are recorded locally and in Commons.

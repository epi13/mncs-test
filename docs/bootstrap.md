# Bootstrap and trust model

`mncs-test` is not claiming that the host boundary has disappeared. The
campaign's current trust chain is:

1. existing Rust/Python tests are Stage 0 bootstrap and independent witnesses;
2. the current `mncs` compiler/runtime is invoked by a narrow adapter;
3. native MNCS assertion and aggregation modules form the semantic test core;
4. the checked-in `tests/self_suite.mncs` is compiled and executed by
   `mncs-test` itself;
5. future releases can remove host tests from the critical path one capability
   at a time, retaining host implementations only as differential or platform
   oracles.

The meaningful semantic core in this repository is MNCS. Python does not
choose property inputs, evaluate predicates, fold suite results, or translate
an assertion into PASS. It launches the compiler, carries bounded JSON/TOML,
and preserves evidence because the current external MNCS profile has no
portable process/session/diagnostic transport API.

The remaining bootstrap boundary is therefore exact and auditable:

```text
mncs-test adapter → external `mncs execute` process → native MNCS functions
```

The next self-hosting milestone is a native execution/session entrypoint that
can run a suite and its entries without one host process per invocation. The
corresponding language pressures are recorded locally and in Commons.

# Bootstrap and trust model

`mncs-test` claims a native-canonical application path while keeping the Rust
compiler/runtime/toolchain as the deliberate bootstrap exception. The current
trust chain is:

1. existing Rust/Python tests are Stage 0 bootstrap and independent witnesses;
2. the current `mncs` compiler owns first-class declaration identity and the
   structural test inventory;
3. native MNCS assertion and aggregation modules form the semantic test core;
4. the checked-in `tests/self_suite.mncs` is inventoried, compiled once, and
   executed through a retained `mncs-embed` session by the compiler-owned
   `mncs test` entrypoint;
5. the Python runner is an explicit compatibility/differential oracle, not a
   fallback selected by the native path.

The meaningful semantic core in this repository is MNCS. Python does not
choose property inputs, evaluate predicates, or translate an assertion into
PASS. For first-class runtime tests it carries the returned native values
through `mncs.test.suite::empty` and `observe` in the retained session;
the native module folds suite results. JSON/TOML, OS process supervision, and
the external structured transport remain platform boundaries.

The remaining bootstrap boundary is therefore exact and auditable:

```text
Rust MNCS toolchain → compiler inventory → retained mncs-embed Session
                   → native MNCS functions → result/check projection
```

If the native toolchain cannot be loaded, the command fails closed. The
legacy subprocess path is available only through `bin/mncs-test-compat`; its
corresponding language pressures are recorded locally and in Commons.

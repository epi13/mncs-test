# Language-pressure log

These records are the `mncs-test` view of limitations encountered while
building an external MNCS consumer. They are not a second source of truth:
each record names its Commons pressure counterpart, and the central Commons
observation is the coordination record.

A pressure is not an excuse for a broad host-language implementation. The
local `disposition` says whether the current release is blocked, contained by
a narrow adapter, or repaired. `status = "open"` remains honest until the
owning MNCS layer supplies a capability and the workaround is removed.

The current campaign records:

| Local ID | Commons record | Owning layer | Disposition |
| --- | --- | --- | --- |
| `MNCS-TEST-P-001` | `MNCS-TOOLING-B665F138D324` | runtime/tooling | compiler launch + explicit fallback adapter |
| `MNCS-TEST-P-002` | `MNCS-TOOLING-C5FD211E05C1` | tooling/filesystem | explicit project manifests; compiler-owned runtime discovery |
| `MNCS-TEST-P-003` | `MNCS-TOOLING-E920D54703E3` | compiler/tooling | structured CLI diagnostic adapter |
| `MNCS-TEST-P-004` | `MNCS-LANG-52A5E0C72A39` | runtime/tooling | retained embed session; fallback remains |
| `MNCS-TEST-P-005` | `MNCS-LANG-770B42F56E6C` | language semantics | first-class static declarations; advanced callbacks remain constrained |
| `MNCS-TEST-P-006` | `MNCS-TOOLING-146FB6F4BBC6` | stdlib/tooling | narrow typed-to-JSON transport adapter |
| `MNCS-TEST-P-008` | _pending_ | family contracts | local external-executor predicate until checkouts reunite |
| `MNCS-TEST-P-009` | _pending_ | doctor | DOC102 on current profile 0.18 is upstream staleness; ignored |
| `MNCS-TEST-P-010` | _pending_ | toolchain | missing `use mncs.test.suite` needs a real diagnostic; documented |
| `MNCS-TEST-P-011` | _pending_ | toolchain packaging | rebuild libmncs_embed.so with the binary; needs a version gate |
| `MNCS-TEST-P-012` | _pending_ | toolchain cache | cache key collides across debug/release; clear or namespace per toolchain |
| `MNCS-TEST-P-013` | _pending_ | forge/actions | family proof still invokes legacy `run --manifest`; native envelopes validate |
| `MNCS-TEST-P-014` | _pending_ | compiler inventory | no resolved module closure; contract requested, host superset stands |
| `MNCS-TEST-P-015` | closed | test evidence admission | vault-head authority; projections repaired; fail-closed outage |
| `MNCS-TEST-P-016` | closed | test coherence policy | lineage recall with native second-pass admission |

When a pressure is repaired, update both this record and its Commons state,
add a regression/conformance witness, and remove the workaround before
changing `status` to `repaired`.

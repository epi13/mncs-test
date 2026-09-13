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
| `MNCS-TEST-P-001` | `MNCS-TOOLING-B665F138D324` | runtime/tooling | narrow process adapter |
| `MNCS-TEST-P-002` | `MNCS-TOOLING-C5FD211E05C1` | tooling/filesystem | explicit manifests |
| `MNCS-TEST-P-003` | `MNCS-TOOLING-E920D54703E3` | compiler/tooling | structured CLI adapter |
| `MNCS-TEST-P-004` | `MNCS-LANG-52A5E0C72A39` | runtime/tooling | one request per process |
| `MNCS-TEST-P-005` | `MNCS-LANG-770B42F56E6C` | language semantics | manifest + static sequence |
| `MNCS-TEST-P-006` | `MNCS-TOOLING-146FB6F4BBC6` | stdlib/tooling | JSON transport adapter |

When a pressure is repaired, update both this record and its Commons state,
add a regression/conformance witness, and remove the workaround before
changing `status` to `repaired`.

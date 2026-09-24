#!/usr/bin/env python3
"""Check the native provider's generated inventory binding against the compiler."""

from __future__ import annotations

import argparse
import os
import sys
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mncs", required=True, type=Path)
    parser.add_argument("--source", action="append", type=Path, default=[])
    parser.add_argument("--library", action="append", type=Path, default=[])
    args = parser.parse_args()
    generator = Path(__file__).with_name("generate_provider.py")
    completed = subprocess.run(
        [
            sys.executable,
            str(generator),
            "--mncs",
            str(args.mncs),
            *sum((["--source", str(source)] for source in args.source), []),
            *sum((["--library", str(path)] for path in args.library), []),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or completed.stdout or "generated provider check failed")
    print(completed.stdout, end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

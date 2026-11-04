"""Disposable transport cache placement, independent of native test semantics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import mncs_test_native


class NativeCachePlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mncs-cache-placement-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.git("init", "-b", "main")
        (self.repository / "README").write_text("cache placement fixture\n")
        self.git("add", "README")
        self.git("-c", "user.name=Cache Test", "-c", "user.email=cache@example.invalid", "commit", "-m", "fixture")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repository), *arguments], text=True, stderr=subprocess.DEVNULL,
        ).strip()

    def cache_at(self, checkout: Path, executable: str = "/toolchains/debug/mncs") -> Path:
        with mock.patch.object(mncs_test_native, "repo_root", return_value=checkout):
            return mncs_test_native.toolchain_cache_dir(executable)

    def test_linked_worktrees_share_cache_without_creating_checkout_local_data(self) -> None:
        linked = self.base / "linked"
        self.git("worktree", "add", "-b", "parallel", str(linked))
        main_cache = self.cache_at(self.repository)
        self.assertEqual(main_cache, self.cache_at(linked))
        self.assertTrue(main_cache.is_relative_to(self.repository / ".git"))
        self.assertFalse((linked / "native-applications/.mncs").exists())

    def test_debug_and_release_namespaces_remain_separate(self) -> None:
        self.assertNotEqual(
            self.cache_at(self.repository, "/toolchains/debug/mncs"),
            self.cache_at(self.repository, "/toolchains/release/mncs"),
        )

    def test_separate_repositories_do_not_share_write_scope(self) -> None:
        other = self.base / "other"
        subprocess.check_call(["git", "init", "-q", str(other)])
        self.assertNotEqual(self.cache_at(self.repository), self.cache_at(other))

    def test_exported_source_and_read_only_common_directory_use_local_cache(self) -> None:
        exported = self.base / "exported"
        exported.mkdir()
        self.assertTrue(self.cache_at(exported).is_relative_to(exported / "native-applications/.mncs/toolchains"))
        with mock.patch.object(mncs_test_native.os, "access", return_value=False):
            self.assertTrue(self.cache_at(self.repository).is_relative_to(self.repository / "native-applications/.mncs/toolchains"))

    def test_explicit_environment_cache_is_forwarded_without_inferred_shared_writes(self) -> None:
        descriptor_root = self.repository / "native-applications"
        descriptor_root.mkdir()
        source = self.repository / "app.mncs"
        source.write_text("mncs 0.6;\nmodule app;\n")
        (descriptor_root / "app.json").write_text(json.dumps({"source": str(source), "libraries": []}))
        environment = {**os.environ, "MNCS_NATIVE_APPLICATION_CACHE_DIR": str(self.base / "selected-cache")}

        def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertNotIn("--cache-dir", command)
            self.assertEqual(kwargs["env"], environment)
            (self.repository / command[-1]).write_text(json.dumps({"schema_version": "fixture.result/1"}))
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(mncs_test_native, "repo_root", return_value=self.repository),
            mock.patch.object(mncs_test_native, "toolchain_cache_dir", side_effect=AssertionError("explicit cache wins")),
            mock.patch.object(mncs_test_native.subprocess, "run", side_effect=execute),
        ):
            result = mncs_test_native.run_native_app(
                "app.json", {"schema_version": "fixture.request/1"},
                request_schema="fixture.request/1", result_schema="fixture.result/1",
                mncs="/toolchains/mncs", cwd=self.repository, environment=environment,
            )
        self.assertEqual(result["schema_version"], "fixture.result/1")

    def test_inferred_namespaces_receive_one_shared_storage_budget(self) -> None:
        descriptor_root = self.repository / "native-applications"
        descriptor_root.mkdir()
        source = self.repository / "app.mncs"
        source.write_text("mncs 0.6;\nmodule app;\n")
        (descriptor_root / "app.json").write_text(json.dumps({"source": str(source), "libraries": []}))
        environment = {}
        selected = self.cache_at(self.repository)

        def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[command.index("--cache-dir") + 1], str(selected))
            self.assertEqual(kwargs["env"], {
                "MNCS_NATIVE_APPLICATION_CACHE_STORAGE_ROOT": str(selected.parent),
            })
            (self.repository / command[-1]).write_text(json.dumps({"schema_version": "fixture.result/1"}))
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(mncs_test_native, "repo_root", return_value=self.repository),
            mock.patch.object(mncs_test_native, "toolchain_cache_dir", return_value=selected),
            mock.patch.object(mncs_test_native.subprocess, "run", side_effect=execute),
        ):
            mncs_test_native.run_native_app(
                "app.json", {"schema_version": "fixture.request/1"},
                request_schema="fixture.request/1", result_schema="fixture.result/1",
                mncs="/toolchains/debug/mncs", cwd=self.repository, environment=environment,
            )
        self.assertEqual(environment, {})


if __name__ == "__main__":
    unittest.main()

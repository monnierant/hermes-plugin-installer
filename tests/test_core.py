from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugin_installer.core import (
    ConsentError,
    InstallFailed,
    InstallerError,
    _bounded_index_metadata,
    _git_url,
    _safe_source,
    classify_source,
    consent_phrase,
    install_verified,
    preview_git_source,
    preview_local_directory,
    preview_source,
    search_plugins,
)


class SourceClassificationTests(unittest.TestCase):
    def test_index_name(self) -> None:
        self.assertEqual(classify_source("demo-plugin"), "index")

    def test_git_repository(self) -> None:
        self.assertEqual(classify_source("owner/repository"), "git")
        self.assertEqual(classify_source("https://github.com/owner/repository.git"), "git")

    def test_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(classify_source(directory), "local-directory")

    def test_owner_repo_is_normalized_for_static_git_clone(self) -> None:
        self.assertEqual(
            _git_url("NousResearch/hermes-media-studio"),
            "https://github.com/NousResearch/hermes-media-studio.git",
        )

    def test_refuses_url_query_fragment_and_control_characters(self) -> None:
        unsafe_sources = (
            "https://example.com/repo.git?token=SECRET",
            "https://example.com/repo.git#access_token=SECRET",
            "SECRET@example.com:org/repo.git",
            "https://example.com/repo.git\n--upload-pack=evil",
        )
        for source in unsafe_sources:
            with self.subTest(source=source):
                with self.assertRaisesRegex(Exception, "refused"):
                    _safe_source(source)

    def test_refuses_direct_file_url(self) -> None:
        with self.assertRaisesRegex(InstallerError, "local file URL"):
            _safe_source("file:///tmp/unapproved-plugin")

    def test_refuses_query_and_fragment_even_without_a_url_scheme(self) -> None:
        unsafe_sources = (
            "owner/repository#access_token=SECRET",
            "//host/repository?token=SECRET",
        )
        for source in unsafe_sources:
            with self.subTest(source=source):
                with self.assertRaisesRegex(InstallerError, "query strings and fragments"):
                    classify_source(source)


class PreviewTests(unittest.TestCase):
    def test_refuses_unsafe_index_ref_and_subdirectory_before_clone(self) -> None:
        runner = Mock()
        with self.assertRaises(InstallerError):
            preview_git_source("owner/repo", ref="--upload-pack=bad", runner=runner)
        with self.assertRaises(InstallerError):
            preview_git_source("owner/repo", subdir="../outside", runner=runner)
        runner.assert_not_called()

    def test_preview_reports_components_and_important_files_without_importing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text(
                "name: safe-demo\nversion: 1.2.3\nrequires_env: [DEMO_TOKEN]\n"
                "pip_dependencies: [httpx]\nhooks: [on_session_end]\n",
                encoding="utf-8",
            )
            (root / "__init__.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            preview = preview_local_directory(root, source_label=str(root))

        self.assertEqual(preview["name"], "safe-demo")
        self.assertEqual(preview["version"], "1.2.3")
        self.assertEqual(preview["requires_env"], ["DEMO_TOKEN"])
        self.assertEqual(preview["pip_dependencies"], ["httpx"])
        self.assertEqual(preview["hooks"], ["on_session_end"])
        self.assertEqual(preview["capabilities"], [])
        self.assertEqual(preview["permissions"], [])
        self.assertIn("plugin.yaml", preview["important_files"])
        self.assertIn("__init__.py", preview["important_files"])
        self.assertFalse(preview["trusted"])

    def test_refuses_hostile_plugin_name_and_yaml_aliases(self) -> None:
        manifests = (
            "name: --all\n",
            "name: safe-demo\npermissions: &many [a, b]\ncapabilities: *many\n",
        )
        for manifest in manifests:
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "plugin.yaml").write_text(manifest, encoding="utf-8")
                with self.assertRaises(InstallerError):
                    preview_local_directory(root, source_label=str(root))

    def test_bounds_manifest_values_and_index_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text(
                "name: safe-demo\ndescription: '" + ("x" * 5000) + "'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InstallerError, "limits"):
                preview_local_directory(root, source_label=str(root))

        projected = _bounded_index_metadata({
            "name": "demo",
            "repo": "owner/repository",
            "description": "ok",
            "unexpected_secret": "must-not-be-copied",
        })
        self.assertEqual(projected, {
            "name": "demo", "repo": "owner/repository", "description": "ok",
        })

    def test_refuses_option_like_repository_from_index_before_git_clone(self) -> None:
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            if command[:3] == ["hermes", "plugins", "search"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({
                        "source": "remote",
                        "results": [{"name": "hostile", "repo": "--upload-pack=evil"}],
                    }),
                    "",
                )
            raise AssertionError(f"unexpected subprocess: {command}")

        with self.assertRaisesRegex(InstallerError, "option-like"):
            preview_source("hostile", runner=runner, hermes_command="hermes")

        self.assertFalse(any(command and command[0] == "git" for command in commands))

    def test_git_preview_records_exact_commit_and_binds_consent_to_it(self) -> None:
        sha = "a" * 40

        def runner(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                root = Path(command[-1])
                (root / "plugin.yaml").write_text("name: safe-demo\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, sha + "\n", "")
            raise AssertionError(command)

        preview = preview_git_source("owner/repository", runner=runner)

        self.assertEqual(preview["commit_sha"], sha)
        self.assertEqual(preview["install_source"], "owner/repository")
        self.assertEqual(consent_phrase(preview), f"INSTALL owner/repository@{sha}")


class SearchTests(unittest.TestCase):
    def test_refuses_option_like_query_and_non_object_json(self) -> None:
        runner = Mock()
        with self.assertRaises(InstallerError):
            search_plugins("--help", runner=runner)
        runner.assert_not_called()

        runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr="",
        )
        with self.assertRaises(InstallerError):
            search_plugins("safe", runner=runner)

    def test_search_delegates_to_official_cli_and_keeps_trust_warning(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"source": "remote", "results": [{"name": "demo"}]}),
            stderr="",
        ))

        with patch.dict(os.environ, {"SHOULD_NOT_LEAK": "secret"}, clear=False):
            result = search_plugins("demo", runner=runner, hermes_command="hermes")

        runner.assert_called_once()
        self.assertEqual(
            runner.call_args.args[0],
            ["hermes", "plugins", "search", "demo", "--json"],
        )
        self.assertNotIn("SHOULD_NOT_LEAK", runner.call_args.kwargs["env"])
        self.assertEqual(result["results"][0]["name"], "demo")
        self.assertIn("not a code audit", result["trust_notice"])

    def test_subprocesses_do_not_inherit_an_ambient_path(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"source": "remote", "results": []}), stderr="",
        ))

        with patch.dict(os.environ, {"PATH": "/tmp/attacker-controlled"}, clear=False):
            search_plugins("demo", runner=runner, hermes_command="hermes")

        subprocess_path = runner.call_args.kwargs["env"]["PATH"]
        self.assertNotIn("attacker-controlled", subprocess_path)
        self.assertTrue(subprocess_path.endswith("/usr/bin:/bin"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preview = {
            "name": "safe-demo",
            "canonical_source": "owner/repository",
            "install_source": "owner/repository",
            "source_kind": "git",
            "commit_sha": "a" * 40,
        }

    @staticmethod
    def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def exact_consent(self) -> str:
        return f"INSTALL owner/repository@{'a' * 40}"

    def test_refuses_missing_exact_consent_without_running_commands(self) -> None:
        runner = Mock()
        with self.assertRaises(ConsentError):
            install_verified(self.preview, "yes", runner=runner, hermes_command="hermes")
        runner.assert_not_called()

    def test_consent_binds_the_exact_install_source_including_subdir(self) -> None:
        preview = {
            **self.preview,
            "canonical_source": "index-entry",
            "install_source": "owner/repository/plugins/demo",
        }
        self.assertEqual(
            consent_phrase(preview),
            f"INSTALL owner/repository/plugins/demo@{'a' * 40}",
        )

        runner = Mock()
        with self.assertRaises(ConsentError):
            install_verified(
                preview,
                f"INSTALL index-entry@{'a' * 40}",
                runner=runner,
            )
        runner.assert_not_called()

    def test_refuses_install_without_an_immutable_commit(self) -> None:
        runner = Mock()
        preview = {**self.preview, "commit_sha": ""}
        with self.assertRaisesRegex(InstallFailed, "immutable commit"):
            install_verified(preview, "INSTALL owner/repository@UNPINNED", runner=runner)
        runner.assert_not_called()

    def test_install_is_pinned_disabled_then_doctor_is_strict(self) -> None:
        runner = Mock(side_effect=[
            self.completed("[]"),
            self.completed("installed"),
            self.completed("healthy"),
        ])

        result = install_verified(
            self.preview, self.exact_consent(), runner=runner, hermes_command="hermes",
        )

        self.assertEqual(runner.call_args_list[0].args[0], ["hermes", "plugins", "list", "--json"])
        self.assertEqual(runner.call_args_list[1].args[0], [
            "hermes", "plugins", "install", "owner/repository",
            "--ref", "a" * 40, "--no-enable",
        ])
        self.assertEqual(runner.call_args_list[2].args[0], [
            "hermes", "plugins", "doctor", "safe-demo", "--ci",
        ])
        self.assertEqual(result["status"], "verified-disabled")
        self.assertEqual(result["commit_sha"], "a" * 40)

    def test_local_source_is_passed_as_pinned_file_url(self) -> None:
        runner = Mock(side_effect=[
            self.completed("true\n"), self.completed("[]"), self.completed(), self.completed(),
        ])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".git").mkdir()
            preview = {
                "name": "safe-demo",
                "canonical_source": directory,
                "install_source": Path(directory).resolve().as_uri(),
                "source_kind": "local-directory",
                "commit_sha": "b" * 40,
            }
            install_verified(
                preview,
                f"INSTALL {Path(directory).resolve().as_uri()}@{'b' * 40}",
                runner=runner,
                hermes_command="hermes",
            )

        self.assertEqual(runner.call_args_list[0].args[0], [
            "git", "-C", str(Path(directory).resolve()), "rev-parse", "--is-inside-work-tree",
        ])
        self.assertEqual(runner.call_args_list[2].args[0], [
            "hermes", "plugins", "install", Path(directory).resolve().as_uri(),
            "--ref", "b" * 40, "--no-enable",
        ])

    def test_non_git_local_source_fails_before_official_install(self) -> None:
        runner = Mock(return_value=self.completed(returncode=128))
        with tempfile.TemporaryDirectory() as directory:
            preview = {
                "name": "safe-demo",
                "canonical_source": directory,
                "install_source": Path(directory).resolve().as_uri(),
                "source_kind": "local-directory",
                "commit_sha": "b" * 40,
            }
            with self.assertRaisesRegex(InstallFailed, "not a Git repository"):
                install_verified(
                    preview,
                    f"INSTALL {Path(directory).resolve().as_uri()}@{'b' * 40}",
                    runner=runner,
                    hermes_command="hermes",
                )
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0][:2], ["git", "-C"])

    def test_fake_git_marker_is_refused_before_official_install(self) -> None:
        runner = Mock(side_effect=[self.completed("[]"), self.completed(), self.completed()])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".git").write_text("not a repository", encoding="utf-8")
            preview = {
                "name": "safe-demo",
                "canonical_source": directory,
                "install_source": Path(directory).resolve().as_uri(),
                "source_kind": "local-directory",
                "commit_sha": "b" * 40,
            }
            with self.assertRaisesRegex(InstallFailed, "not a Git repository"):
                install_verified(
                    preview,
                    f"INSTALL {Path(directory).resolve().as_uri()}@{'b' * 40}",
                    runner=runner,
                    hermes_command="hermes",
                )

        self.assertFalse(any(call.args[0][:3] == ["hermes", "plugins", "install"] for call in runner.call_args_list))

    def test_refuses_name_conflict_before_install(self) -> None:
        runner = Mock(return_value=self.completed(json.dumps([{"name": "safe-demo"}])))
        with self.assertRaisesRegex(InstallFailed, "already exists"):
            install_verified(
                self.preview, self.exact_consent(), runner=runner, hermes_command="hermes",
            )
        runner.assert_called_once()

    def test_refuses_hostile_name_before_running_commands(self) -> None:
        runner = Mock()
        preview = {**self.preview, "name": "--all"}
        with self.assertRaisesRegex(InstallFailed, "plugin name"):
            install_verified(preview, self.exact_consent(), runner=runner)
        runner.assert_not_called()

    def test_failed_doctor_never_removes_state_that_may_have_changed_concurrently(self) -> None:
        runner = Mock(side_effect=[
            self.completed("[]"),
            self.completed("installed"),
            self.completed(returncode=4),
        ])

        with self.assertRaisesRegex(InstallFailed, "left disabled"):
            install_verified(
                self.preview, self.exact_consent(), runner=runner, hermes_command="hermes",
            )

        self.assertEqual(len(runner.call_args_list), 3)
        self.assertNotIn("remove", runner.call_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()

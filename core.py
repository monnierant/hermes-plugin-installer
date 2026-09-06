"""Safe discovery, static preview, and verified installation orchestration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

Runner = Callable[..., subprocess.CompletedProcess[str]]
IMPORTANT_NAMES = {
    "plugin.yaml", "__init__.py", "requirements.txt", "pyproject.toml",
    "package.json", "README.md", "README.rst", "LICENSE", "LICENSE.txt",
    "AGENTS.md", "SKILL.md",
}
PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
INDEX_METADATA_KEYS = (
    "name", "repo", "repository", "ref", "subdir", "version", "description", "author",
)
SUBPROCESS_ENV_KEYS = (
    "HOME", "HERMES_HOME", "HERMES_PROFILE", "TMPDIR", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "XDG_CONFIG_HOME",
)
TRUSTED_PATH = os.pathsep.join((str(Path(sys.executable).absolute().parent), "/usr/bin", "/bin"))
MANIFEST_LIST_KEYS = (
    "capabilities",
    "permissions",
    "provides_tools", "hooks", "skills", "commands", "requires_env",
    "pip_dependencies", "external_dependencies",
)


class InstallerError(RuntimeError):
    """Base error with an actionable operator-facing message."""


class ConsentError(InstallerError):
    pass


class InstallFailed(InstallerError):
    pass


def _run(runner: Runner, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    environment = {key: os.environ[key] for key in SUBPROCESS_ENV_KEYS if key in os.environ}
    environment["PATH"] = TRUSTED_PATH
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_ALLOW_PROTOCOL": "https:ssh:git:file",
    })
    return runner(
        command, capture_output=True, text=True, timeout=timeout, check=False,
        env=environment,
    )


def _safe_source(source: str, *, allow_local_file: bool = False) -> str:
    value = source.strip()
    if not value:
        raise InstallerError("source is required")
    if len(value) > 2048:
        raise InstallerError("source refused: source exceeds the length limit")
    if value != source or any(ord(character) < 32 or ord(character) == 127 for character in source):
        raise InstallerError("source refused: leading/trailing whitespace and control characters are not allowed")
    if value.startswith("-"):
        raise InstallerError("source refused: option-like repository sources are forbidden")
    parsed = urlparse(value)
    if parsed.username or parsed.password:
        raise InstallerError("source refused: credentials embedded in a source URL are forbidden")
    if re.match(r"^[^/@:\s]+@[^/\s]+:.+", value) and not value.startswith("git@"):
        raise InstallerError("source refused: credentials embedded in an SCP-style Git source are forbidden")
    if parsed.query or parsed.fragment:
        raise InstallerError(
            "source refused: URL query strings and fragments may expose credentials; "
            "use a credential helper and an explicit repository path"
        )
    if parsed.scheme == "file" and not allow_local_file:
        raise InstallerError("source refused: a local file URL is not an approved remote source")
    if parsed.scheme == "file" and (
        parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/")
    ):
        raise InstallerError("source refused: local file URL must identify an absolute local path")
    return value


def classify_source(source: str) -> str:
    value = _safe_source(source)
    path = Path(value).expanduser()
    if path.exists():
        return "local-directory" if path.is_dir() else "local-archive"
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "ssh", "git", "file"}:
        lower_path = parsed.path.lower()
        if lower_path.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
            return "remote-archive"
        return "git"
    if re.fullmatch(r"[^/\s]+/[^/\s]+(?:/[^/\s]+)?", value):
        return "git"
    return "index"


def search_plugins(
    query: str = "", *, runner: Runner = subprocess.run, hermes_command: str = "hermes",
) -> dict[str, Any]:
    if (
        len(query) > 256
        or query.startswith("-")
        or query != query.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
    ):
        raise InstallerError("search query must be bounded text and may not resemble an option")
    command = [hermes_command, "plugins", "search"]
    if query:
        command.append(query)
    command.append("--json")
    completed = _run(runner, command, timeout=30)
    if completed.returncode:
        raise InstallerError(f"Hermes index search failed (exit {completed.returncode}).")
    if len(completed.stdout) > 1_000_000:
        raise InstallerError("Hermes index search response exceeds the 1 MB limit.")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallerError("Hermes index search returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise InstallerError("Hermes index search returned an unexpected JSON structure.")
    payload["trust_notice"] = (
        "Index inclusion and static inspection are not a code audit; source trust remains unverified."
    )
    return payload


def _normalise_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _validate_bounded_data(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 512:
        raise InstallerError("plugin.yaml exceeds structural inspection limits")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if len(value) > 4096:
            raise InstallerError("plugin.yaml exceeds string inspection limits")
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise InstallerError("plugin.yaml exceeds list inspection limits")
        for item in value:
            _validate_bounded_data(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise InstallerError("plugin.yaml exceeds mapping inspection limits")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise InstallerError("plugin.yaml contains an invalid or oversized key")
            _validate_bounded_data(item, depth=depth + 1, budget=budget)
        return
    raise InstallerError("plugin.yaml contains an unsupported typed value")


def _validated_plugin_name(value: Any, *, install: bool = False) -> str:
    name = str(value or "").strip()
    if not PLUGIN_NAME_RE.fullmatch(name):
        error = InstallFailed if install else InstallerError
        raise error("plugin name must be a bounded ASCII identifier")
    return name


def _bounded_index_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in INDEX_METADATA_KEYS:
        if key not in entry:
            continue
        value = entry[key]
        if value is None or isinstance(value, (bool, int, float)):
            projected[key] = value
        elif isinstance(value, str) and len(value) <= 4096 and not any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            projected[key] = value
    return projected


def _important_files(root: Path) -> list[str]:
    files: list[str] = []
    stack = [root]
    scanned = 0
    try:
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > 2000:
                        raise InstallerError("plugin tree exceeds the 2000-entry inspection limit")
                    if entry.name == ".git":
                        continue
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        relative = path.relative_to(root).as_posix()
                        if len(relative) > 512:
                            raise InstallerError("plugin tree contains an oversized path")
                        if path.name in IMPORTANT_NAMES or relative.startswith(
                            ("skills/", "tools/", "hooks/", "commands/")
                        ):
                            files.append(relative)
                            if len(files) > 256:
                                raise InstallerError(
                                    "plugin tree exceeds the important-file inspection limit"
                                )
    except OSError as exc:
        raise InstallerError("plugin tree cannot be traversed safely") from exc
    return sorted(files)


def preview_local_directory(root: Path, *, source_label: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    manifest_path = root / "plugin.yaml"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise InstallerError("plugin.yaml is missing or is a symlink")
    if manifest_path.stat().st_size > 65_536:
        raise InstallerError("plugin.yaml exceeds the 64 KiB inspection limit")
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        for token in yaml.scan(manifest_text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise InstallerError(
                    "plugin.yaml aliases and anchors are not accepted for static inspection"
                )
        manifest = yaml.safe_load(manifest_text) or {}
    except InstallerError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise InstallerError("plugin.yaml cannot be read safely") from exc
    if not isinstance(manifest, dict):
        raise InstallerError("plugin.yaml must contain a mapping")
    _validate_bounded_data(manifest)
    name = _validated_plugin_name(manifest.get("name"))
    files = _important_files(root)

    preview: dict[str, Any] = {
        "name": name,
        "version": str(manifest.get("version") or "unspecified"),
        "description": str(manifest.get("description") or ""),
        "author": str(manifest.get("author") or ""),
        "source": source_label,
        "canonical_source": source_label,
        "source_kind": "local-directory",
        "trusted": False,
        "trust_notice": "Static metadata inspection only; plugin code was not imported or executed.",
        "important_files": files,
    }
    for key in MANIFEST_LIST_KEYS:
        preview[key] = _normalise_list(manifest.get(key))
    return preview


def _git_url(source: str) -> str:
    if re.fullmatch(r"[^/\s]+/[^/\s]+(?:/[^/\s]+)?", source):
        owner, repository, *_ = source.split("/")
        return f"https://github.com/{owner}/{repository}.git"
    return source


def preview_git_source(
    source: str, *, subdir: str | None = None, ref: str | None = None,
    runner: Runner = subprocess.run, allow_local_file: bool = False,
) -> dict[str, Any]:
    safe_source = _safe_source(source, allow_local_file=allow_local_file)
    if ref and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref)
        or ".." in ref or "@{" in ref or "//" in ref or ref.endswith(("/", ".lock"))
    ):
        raise InstallerError("index ref is not a safe bounded Git revision")
    if subdir and (
        len(subdir) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in subdir)
        or Path(subdir).is_absolute()
        or ".." in Path(subdir).parts
    ):
        raise InstallerError("index subdirectory is not a safe bounded relative path")
    with tempfile.TemporaryDirectory(prefix="hermes-plugin-preview-") as directory:
        command = ["git", "clone", "--no-tags"]
        if not ref:
            command.extend(["--depth", "1"])
        command.extend(["--", _git_url(safe_source), directory])
        completed = _run(runner, command, timeout=60)
        if completed.returncode:
            raise InstallerError(
                f"Static Git preview failed (exit {completed.returncode}); "
                "verify that the repository exists, is reachable without credentials, "
                "and that its index ref is still available."
            )
        if ref:
            checkout = _run(
                runner,
                ["git", "-C", directory, "checkout", "--detach", ref],
                timeout=30,
            )
            if checkout.returncode:
                raise InstallerError(
                    f"Static Git preview could not verify the index ref (exit {checkout.returncode})."
                )
        revision = _run(
            runner,
            ["git", "-C", directory, "rev-parse", "HEAD"],
            timeout=30,
        )
        commit_sha = revision.stdout.strip().lower()
        if revision.returncode or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise InstallerError("Static Git preview could not resolve an immutable commit SHA.")
        root = Path(directory)
        if subdir:
            candidate = (root / subdir).resolve()
            if root.resolve() not in candidate.parents:
                raise InstallerError("index subdirectory escapes the cloned repository")
            root = candidate
        preview = preview_local_directory(root, source_label=safe_source)
    preview["source_kind"] = "git"
    preview["canonical_source"] = safe_source
    preview["install_source"] = (
        f"{safe_source.rstrip('/')}/{str(subdir).strip('/')}" if subdir else safe_source
    )
    preview["commit_sha"] = commit_sha
    if ref:
        preview["ref"] = ref
    return preview


def preview_source(
    source: str, *, runner: Runner = subprocess.run, hermes_command: str = "hermes",
) -> dict[str, Any]:
    safe_source = _safe_source(source)
    kind = classify_source(safe_source)
    if kind == "local-directory":
        local_source = Path(safe_source).expanduser().resolve()
        if (local_source / ".git").exists():
            preview = preview_git_source(
                local_source.as_uri(), runner=runner, allow_local_file=True,
            )
            preview["source"] = safe_source
            preview["canonical_source"] = safe_source
            preview["install_source"] = local_source.as_uri()
            preview["source_kind"] = "local-directory"
            return preview
        return preview_local_directory(local_source, source_label=safe_source)
    if kind == "git":
        return preview_git_source(safe_source, runner=runner)
    if kind in {"local-archive", "remote-archive"}:
        raise InstallerError(
            "Archive preview is not available in this MVP; the local Hermes runtime also does not expose archive install in its CLI help."
        )

    index = search_plugins(safe_source, runner=runner, hermes_command=hermes_command)
    results = index.get("results", [])
    if not isinstance(results, list) or len(results) > 1000:
        raise InstallerError("Hermes index search returned an unsafe result set.")
    matches = [
        item for item in results
        if isinstance(item, dict) and item.get("name") == safe_source
    ]
    if len(matches) != 1:
        raise InstallerError("No unique exact plugin name was found in the Hermes index.")
    entry = matches[0]
    repository = str(entry.get("repo") or "")
    if not repository:
        raise InstallerError("The index entry has no repository.")
    preview = preview_git_source(
        repository,
        subdir=entry.get("subdir"),
        ref=entry.get("ref") or None,
        runner=runner,
    )
    preview["canonical_source"] = safe_source
    preview["source_kind"] = "index"
    preview["index_metadata"] = _bounded_index_metadata(entry)
    return preview


def consent_phrase(preview: dict[str, Any]) -> str:
    revision = str(preview.get("commit_sha") or "UNPINNED")
    source_kind = preview.get("source_kind")
    install_source = _safe_source(
        str(preview.get("install_source") or preview["canonical_source"]),
        allow_local_file=source_kind == "local-directory",
    )
    return f"INSTALL {install_source}@{revision}"


def _installed_plugin_names(payload: Any) -> set[str]:
    entries = payload
    if isinstance(payload, dict):
        entries = payload.get("plugins", payload.get("results", []))
    if not isinstance(entries, list):
        raise InstallFailed("Hermes plugin list returned an unexpected structure; installation was not attempted.")
    return {
        str(entry.get("name"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("name")
    }


def install_verified(
    preview: dict[str, Any], consent: str, *, runner: Runner = subprocess.run,
    hermes_command: str = "hermes",
) -> dict[str, Any]:
    source = _safe_source(str(preview["canonical_source"]))
    name = _validated_plugin_name(preview.get("name"), install=True)
    commit_sha = str(preview.get("commit_sha") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise InstallFailed("Installation requires an immutable commit SHA from the static preview.")
    expected = consent_phrase(preview)
    if consent != expected:
        raise ConsentError(f"Consent refused. Exact phrase required: {expected}")

    install_source = str(preview.get("install_source") or source)
    if preview.get("source_kind") == "local-directory":
        local_source = Path(source).resolve()
        git_check = _run(
            runner,
            ["git", "-C", str(local_source), "rev-parse", "--is-inside-work-tree"],
            timeout=15,
        )
        if git_check.returncode or git_check.stdout.strip() != "true":
            raise InstallFailed(
                "The local Hermes CLI installs from Git sources only; this directory is not a Git repository. "
                "Create an approved local commit or use an explicit remote source before installing."
            )
        install_source = local_source.as_uri()
    else:
        try:
            install_source = _safe_source(install_source)
        except InstallerError as exc:
            raise InstallFailed("preview install source is not an approved remote source") from exc

    existing = _run(
        runner,
        [hermes_command, "plugins", "list", "--json"],
        timeout=30,
    )
    if existing.returncode:
        raise InstallFailed(
            f"Hermes plugin conflict check failed (exit {existing.returncode}); installation was not attempted."
        )
    if len(existing.stdout) > 1_000_000:
        raise InstallFailed("Hermes plugin conflict check response exceeds the 1 MB limit.")
    try:
        existing_names = _installed_plugin_names(json.loads(existing.stdout))
    except json.JSONDecodeError as exc:
        raise InstallFailed(
            "Hermes plugin conflict check returned invalid JSON; installation was not attempted."
        ) from exc
    if name in existing_names:
        raise InstallFailed(
            f"Plugin '{name}' already exists; refusing to overwrite or remove pre-existing state."
        )

    install = _run(
        runner,
        [
            hermes_command, "plugins", "install", install_source,
            "--ref", commit_sha, "--no-enable",
        ],
        timeout=180,
    )
    if install.returncode:
        raise InstallFailed(f"Hermes install failed (exit {install.returncode}); plugin was not verified.")

    doctor = _run(
        runner,
        [hermes_command, "plugins", "doctor", name, "--ci"],
        timeout=120,
    )
    if doctor.returncode:
        raise InstallFailed(
            f"Hermes doctor failed (exit {doctor.returncode}); the plugin was left disabled "
            "for manual inspection. Automatic removal is refused because concurrent state "
            "changes cannot be proven absent."
        )

    return {
        "status": "verified-disabled",
        "name": name,
        "source": source,
        "install_source": install_source,
        "commit_sha": commit_sha,
        "doctor_exit": doctor.returncode,
        "next_step": f"Review, then run: {hermes_command} plugins enable {name}",
    }

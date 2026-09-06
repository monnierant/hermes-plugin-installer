# Hermes Plugin Installer — MVP

A narrow operator CLI that reuses Hermes' official plugin commands instead of copying installation logic.

## Safety model

- `search` delegates to `hermes plugins search --json`.
- `preview` reads `plugin.yaml` and important filenames without importing plugin code.
- Git preview resolves and records the exact commit SHA; index inclusion and static inspection are explicitly **not** treated as a code audit.
- Credential-bearing URLs, URL query strings, URL fragments, and control characters are rejected so secrets do not enter argv or logs.
- Direct `file://` input is rejected; the only local-file source is an existing Git directory selected as a local path and canonicalized internally.
- Local installation validates the repository with `git rev-parse --is-inside-work-tree`; a mere `.git` marker is insufficient.
- Subprocesses receive a minimal allowlisted environment and a fixed trusted `PATH` rooted in the active Hermes runtime plus system binary directories. User/system Git configuration, credential helpers, interactive prompts, and ambient secret variables are disabled or omitted.
- Plugin names, repository sources (including option-like arguments), Git refs, index subdirectories, manifest structure, index metadata, subprocess JSON, and filesystem traversal are validated and bounded before use or display.
- `install` prints the preview and requires the exact phrase `INSTALL <source>@<commit-sha>`.
- Installation delegates to `hermes plugins install <resolved-source> --ref <commit-sha> --no-enable` so preview and install are bound to immutable content.
- Existing plugin names are detected before installation and refused.
- The plugin remains disabled and `hermes plugins doctor <name> --ci` must pass.
- A failed doctor fails the operation but leaves the plugin disabled for manual inspection. Automatic removal is deliberately refused because concurrent state changes cannot be excluded atomically.

## Commands

```text
hermes plugin-installer search [query]
hermes plugin-installer preview <index-name|owner/repo|git-url|local-directory>
hermes plugin-installer install <source> [--consent "INSTALL <source>@<commit-sha>"]
```

Interactive consent is preferred. `--consent` is intended for already-approved, auditable automation and still requires an exact source-and-commit-bound phrase.

## Current compromise

This MVP previews index entries, Git repositories, and local directories. Archive preview is rejected explicitly. The installed local Hermes runtime installs only from a Git source: a local directory must therefore be a Git repository, and preview inspects a clone of its committed `HEAD` before delegating it as a `file://` URL. A plain local directory is rejected with an actionable error rather than copied through custom logic.

## Tests

```text
python -m unittest discover -s tests -v
```

No commit, push, publication, or third-party plugin installation is performed by the test suite.

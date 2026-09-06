"""Hermes CLI integration for the safe plugin installer."""

from __future__ import annotations

import argparse
import json

from .core import InstallerError, consent_phrase, install_verified, preview_source, search_plugins


def register_cli(parser: argparse.ArgumentParser) -> None:
    subcommands = parser.add_subparsers(dest="plugin_installer_action")

    search = subcommands.add_parser("search", help="Search the official Hermes plugin index")
    search.add_argument("query", nargs="?", default="")

    preview = subcommands.add_parser("preview", help="Statically inspect a plugin source")
    preview.add_argument("source")

    install = subcommands.add_parser("install", help="Preview, consent, install disabled, and run doctor")
    install.add_argument("source")
    install.add_argument(
        "--consent",
        help="Exact phrase shown by preview (omit for an interactive prompt)",
    )
    parser.set_defaults(func=plugin_installer_command)


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def plugin_installer_command(args: argparse.Namespace) -> int:
    action = getattr(args, "plugin_installer_action", None)
    try:
        if action == "search":
            _print(search_plugins(args.query))
            return 0
        if action == "preview":
            preview = preview_source(args.source)
            preview["required_consent"] = consent_phrase(preview)
            _print(preview)
            return 0
        if action == "install":
            preview = preview_source(args.source)
            preview["required_consent"] = consent_phrase(preview)
            _print(preview)
            consent = args.consent
            if consent is None:
                consent = input("Type the exact required_consent phrase to install disabled: ")
            _print(install_verified(preview, consent))
            return 0
        print("Usage: hermes plugin-installer {search|preview|install}")
        return 2
    except (InstallerError, EOFError, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1

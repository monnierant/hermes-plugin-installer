"""Safe plugin discovery and verified installation plugin."""

from __future__ import annotations

from .cli import plugin_installer_command, register_cli


def register(ctx) -> None:
    ctx.register_cli_command(
        name="plugin-installer",
        help="Discover, preview, and safely install Hermes plugins",
        setup_fn=register_cli,
        handler_fn=plugin_installer_command,
        description=(
            "Searches the Hermes community index, statically previews plugin metadata and files, "
            "requires exact consent, delegates installation to the official Hermes CLI, and runs doctor."
        ),
    )

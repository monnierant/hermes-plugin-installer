"""Inert integration fixture with one no-op operator command."""


def _setup(parser) -> None:
    parser.set_defaults(func=lambda _args: 0)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="installer-integration-fixture",
        help="No-op command used only by plugin_installer integration tests",
        setup_fn=_setup,
        handler_fn=lambda _args: 0,
    )

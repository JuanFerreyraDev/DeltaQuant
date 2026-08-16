"""DeltaQuant — triangular arbitrage bot entry point.

This module is intentionally minimal in Phase 1.  The event loop, exchange
connections, and the full async pipeline are wired in Phase 2+.  For now it
serves as a structural placeholder and a smoke-test that all packages import
correctly.
"""


def main() -> None:
    """Start the DeltaQuant bot.

    Phase 1: imports only — no async loop yet.
    Phase 2+: initialise uvloop, load settings, start WebSocket streams.
    """
    from config.settings import Settings  # noqa: F401 — verify import chain

    print("DeltaQuant Phase 1 scaffold loaded successfully.")


if __name__ == "__main__":
    main()

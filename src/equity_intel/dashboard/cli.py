"""
CLI entry point for the local research dashboard.

Usage::

    equity-dashboard
    equity-dashboard --port 5173
    equity-dashboard --host 0.0.0.0 --port 8080
    equity-dashboard --no-open

Opens http://localhost:5173 automatically in your default browser unless
``--no-open`` is passed.
"""
from __future__ import annotations

import threading
import time
import webbrowser

import click

from equity_intel.dashboard.app import create_app
from equity_intel.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


@click.command("equity-dashboard")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=5173, show_default=True, type=int, help="Bind port.")
@click.option(
    "--no-open",
    is_flag=True,
    default=False,
    help="Do not open the browser automatically.",
)
@click.option(
    "--log-level",
    default="warning",
    show_default=True,
    help="Logging level (debug, info, warning, error).",
)
@click.option(
    "--shutdown-on-idle",
    is_flag=True,
    default=False,
    help=(
        "Exit automatically when the browser tab is closed (no ping received "
        "for 25 s).  Use this when running windowless via pythonw.exe."
    ),
)
def main(host: str, port: int, no_open: bool, log_level: str, shutdown_on_idle: bool) -> None:
    """
    Start the local equity research dashboard.

    Reads from the equity_intel database — run the sync workers first
    to populate data.  The dashboard is a read-only evidence viewer;
    it does not execute trades or provide investment advice.
    """
    configure_logging(log_level)

    app = create_app(shutdown_on_idle=shutdown_on_idle)
    url = f"http://{host}:{port}"

    click.echo(f"  Equity Intelligence Dashboard")
    click.echo(f"  Running at {url}")
    click.echo(f"  Press Ctrl+C to stop.\n")
    click.echo(f"  For research review only -- not investment advice.")

    if not no_open:
        def _open() -> None:
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

"""
Local file delivery adapter.

Writes a catalyst brief to a file on the local filesystem.
This is the default delivery channel used by the daily brief worker.

Supports JSON and Markdown output.  Parent directories are created
automatically.  Writing to an existing path overwrites it (idempotent).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from equity_intel.export.base import DeliveryAdapter
from equity_intel.logging_config import get_logger

logger = get_logger(__name__)


class LocalFileDelivery(DeliveryAdapter):
    """
    Deliver a catalyst brief to a local file.

    Usage::

        from pathlib import Path
        from equity_intel.export import LocalFileDelivery

        adapter = LocalFileDelivery()
        result = adapter.deliver(brief, Path("briefs/brief_20240115.json"), "json")
        assert result["status"] == "ok"
        print(result["destination"])
    """

    def deliver(
        self,
        brief: Dict[str, Any],
        output_path: Path,
        fmt: str,
    ) -> Dict[str, Any]:
        """
        Write the brief to ``output_path`` in the requested format.

        Parameters
        ----------
        brief       : result dict from ``get_watchlist_brief()``
        output_path : file path to write (parent directories are created)
        fmt         : "json" writes indented JSON; "markdown" writes human-readable Markdown

        Returns
        -------
        dict:
            status        : "ok" on success, "error" on failure
            message       : human-readable description
            destination   : absolute path of the written file (str)
            bytes_written : number of bytes written
            fmt           : format used
        """
        try:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            if fmt == "markdown":
                # Lazy import to avoid circular dependency
                from equity_intel.briefs.watchlist import _render_markdown_from_brief  # noqa: PLC0415
                content = _render_markdown_from_brief(brief)
            else:
                content = json.dumps(brief, default=str, indent=2)

            output_path.write_text(content, encoding="utf-8")
            bytes_written = len(content.encode("utf-8"))

            logger.info(
                "local_file_delivery_ok",
                path=str(output_path),
                fmt=fmt,
                bytes_written=bytes_written,
            )

            return {
                "status": "ok",
                "message": f"Brief written to {output_path}",
                "destination": str(output_path),
                "bytes_written": bytes_written,
                "fmt": fmt,
            }

        except Exception as exc:  # pragma: no cover
            logger.error("local_file_delivery_error", path=str(output_path), error=str(exc))
            return {
                "status": "error",
                "message": str(exc),
                "destination": str(output_path),
                "bytes_written": 0,
                "fmt": fmt,
            }

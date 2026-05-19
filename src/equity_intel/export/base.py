"""
Abstract base class for brief delivery adapters.

To add a new delivery channel (email, Slack, webhook, etc.), subclass
:class:`DeliveryAdapter` and implement :meth:`deliver`.

The contract:
- ``deliver()`` receives the full brief dict, a suggested output path,
  and the desired format string ("json" or "markdown").
- It returns a result dict with at minimum:
  - ``status``      : "ok" | "error"
  - ``message``     : human-readable description
  - ``destination`` : where the brief was delivered (file path, URL, channel, etc.)

Adapters must NOT embed ranking, scoring, or formatting logic — that
lives in :mod:`equity_intel.briefs.watchlist` and
:mod:`equity_intel.workers.generate_watchlist_brief`.

Future adapter stubs
--------------------
The following adapters are planned but not yet implemented.  They will
live in this package when added:

* ``email.py``  — SMTP / SendGrid delivery; requires EMAIL_* .env keys.
* ``slack.py``  — Slack Incoming Webhook delivery; requires SLACK_WEBHOOK_URL.
* ``webhook.py`` — Generic HTTP POST delivery; requires WEBHOOK_URL.

None of these are active defaults.  The only active adapter is
:class:`~equity_intel.export.local_file.LocalFileDelivery`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict


class DeliveryAdapter(ABC):
    """
    Abstract base for brief delivery channels.

    Subclasses must implement :meth:`deliver`.
    """

    @abstractmethod
    def deliver(
        self,
        brief: Dict[str, Any],
        output_path: Path,
        fmt: str,
    ) -> Dict[str, Any]:
        """
        Deliver a catalyst brief to its destination.

        Parameters
        ----------
        brief       : result dict from ``get_watchlist_brief()``
        output_path : suggested destination path (may be ignored by some adapters,
                      e.g. a Slack adapter posts to a channel instead of writing a file)
        fmt         : "json" or "markdown"

        Returns
        -------
        dict with at minimum:
            status      : "ok" | "error"
            message     : human-readable description of what happened
            destination : where the brief was delivered (str — file path, URL, etc.)
        """

"""
Brief delivery abstraction.

Provides a :class:`DeliveryAdapter` interface and a built-in
:class:`LocalFileDelivery` implementation that writes JSON or Markdown
brief files to disk.

Future adapters (email, Slack, webhook) can be added here without
changing any calling code in the workers.

Example usage::

    from equity_intel.export import LocalFileDelivery

    adapter = LocalFileDelivery()
    result = adapter.deliver(brief, output_path=Path("briefs/brief_20240115.json"), fmt="json")
    print(result["destination"])  # "briefs/brief_20240115.json"
"""
from equity_intel.export.base import DeliveryAdapter
from equity_intel.export.local_file import LocalFileDelivery

__all__ = [
    "DeliveryAdapter",
    "LocalFileDelivery",
]

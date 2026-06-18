"""Live mode package for Luma.

Exposes the :class:`LiveMode` controller — a proactive, continuously
running frame-by-frame narrator for blind users. See :mod:`.live` for
details.
"""

from .live import LiveMode

__all__ = ["LiveMode"]

"""File URI construction for immutable, published SQLite artifacts.

Connections which attach these URIs must explicitly pass ``uri=True`` to
``sqlite3.connect``. URI handling is a connection property; depending on the
SQLite build's process-wide default makes otherwise identical handoffs fail.
"""

from pathlib import Path


def immutable_sqlite_uri(path: Path) -> str:
    """Encode a filesystem path without treating its bytes as URI syntax."""

    return Path(path).resolve().as_uri() + "?mode=ro&immutable=1"

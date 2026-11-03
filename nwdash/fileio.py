"""Atomic file writes that ride out a briefly held file (review O5).

On Windows `os.replace` fails while another process has the target open
without delete sharing: antivirus scanning a file it just saw change, a backup
agent, an editor. The hold is usually gone within a second, so the replace is
retried a few times. A final failure is logged before it is raised, so a writer
that carries on without the file no longer loses a save silently.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from .config import LOG

# The waits between tries, in seconds: about 1.5 s in all before giving up.
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace, tried again while the target is held. Only PermissionError is
    retried: that is what a held file raises, and nothing else gets better by
    waiting."""
    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` in one step: a reader sees the old file or the new
    one, never part of either. Each write has a temporary file of its own, so
    two threads saving the same file cannot interleave. Creates the folder.
    Raises OSError after logging a warning; the temporary file is removed."""
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        tmp = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        replace_with_retry(tmp, path)
    except OSError as exc:
        LOG.warning(f"could not save {path.name}: {exc}", extra={"event": "file_write_failed"})
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """atomic_write_bytes for text. No newline translation: the file holds
    exactly `text`."""
    atomic_write_bytes(path, text.encode(encoding))

"""Safe, snapshot-based access to authored Markdown files."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO


class ContentNotFound(FileNotFoundError):
    pass


class ContentStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path_for(self, page: int | None) -> Path:
        if page is None or (isinstance(page, int) and page == 100):
            candidate = self.root / "index.md"
        elif isinstance(page, int) and 100 <= page <= 999:
            candidate = self.root / "pages" / f"{page:03d}.md"
        else:
            raise ValueError("invalid page")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ContentNotFound(str(candidate))
        return resolved

    def snapshot(self, page: int | None) -> BinaryIO:
        source_path = self.path_for(page)
        snapshot = tempfile.TemporaryFile(mode="w+b")
        try:
            with source_path.open("rb") as source:
                shutil.copyfileobj(source, snapshot, length=1024 * 1024)
            snapshot.seek(0)
            return snapshot
        except BaseException:
            snapshot.close()
            raise

"""Delete only the active campaign's explicit session directories."""

from __future__ import annotations

import stat
from pathlib import Path


def clear_session_directories(campaign_dir: Path) -> None:
    """Validate the whole deletion plan before touching any file.

    ``session`` is the mobile client's historical name; the engine writes to
    ``sessions``. Links and Windows reparse points are rejected, including
    nested junctions, so cleanup never traverses another campaign.
    """
    campaign = campaign_dir.resolve(strict=True)
    plan: list[tuple[Path, bool]] = []

    def visit(path: Path, root: Path, *, keep: bool = False) -> None:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Session directories must not contain links")
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Session path is outside the campaign")
        directory = stat.S_ISDIR(info.st_mode)
        if directory:
            for child in path.iterdir():
                visit(child, root)
        elif keep or not stat.S_ISREG(info.st_mode):
            raise ValueError("Invalid session directory entry")
        if not keep:
            plan.append((path, directory))

    for name in ("session", "sessions"):
        root = campaign / name
        # lstat also detects broken links, unlike exists().
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        visit(root, root, keep=True)

    for path, directory in plan:
        if directory:
            path.rmdir()
        else:
            path.unlink()

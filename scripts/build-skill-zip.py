#!/usr/bin/env python3
"""Build ``dist/urcap-skill.zip``, the file a Claude app user uploads as a custom skill.

The zip holds one folder, ``urcap/``: the skill (``SKILL.md`` + ``references/``) and a copy
of the ``urcapgen`` package beside it, so the skill works where nothing is installed
(``python3 -m urcapgen`` with the skill's folder on ``PYTHONPATH``). Reproducible: sorted
entries, fixed timestamps.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "urcapgen" / "template" / ".claude" / "skills" / "urcap"
PACKAGE = ROOT / "urcapgen"
FIXED = (2026, 1, 1, 0, 0, 0)


def files() -> list[tuple[str, Path]]:
    out = [(f"urcap/{p.relative_to(SKILL).as_posix()}", p) for p in SKILL.rglob("*") if p.is_file()]
    for p in PACKAGE.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            out.append((f"urcap/urcapgen/{p.relative_to(PACKAGE).as_posix()}", p))
    return sorted(out)


def build(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in files():
            info = zipfile.ZipInfo(name, FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            z.writestr(info, path.read_bytes())
    return dest


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "urcap-skill.zip")
    print(out)

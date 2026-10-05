"""urcap-skill.zip — what Claude app users upload — must pass Claude's uploader (exactly one
SKILL.md) and still set up complete URCap monorepos from its bundled urcapgen."""

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SkillZip(unittest.TestCase):
    def test_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            zpath = t / "urcap-skill.zip"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "build-skill-zip.py"), str(zpath)], check=True,
                           capture_output=True)  # fmt: skip
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                z.extractall(t / "unzipped")
            self.assertEqual([n for n in names if n.endswith("SKILL.md")], ["urcap/SKILL.md"])
            self.assertEqual({n.split("/")[0] for n in names}, {"urcap"})

            env = {"PYTHONPATH": str(t / "unzipped" / "urcap"), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
            mono = t / "mono"
            r = subprocess.run([sys.executable, "-m", "urcapgen", "init", str(mono)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            for p in (".claude/skills/urcap/SKILL.md", ".claude/skills/urcap/references/spec.md",
                      "tools/urcapgen/template/.claude/skills/urcap/SKILL.md"):  # fmt: skip
                self.assertTrue((mono / p).is_file(), p)
            self.assertFalse((mono / ".claude/skills/urcap/urcapgen").exists())

            # the monorepo's own copy works, and upgrades without losing the skill
            run = lambda *a: subprocess.run([str(mono / "urcapgen"), *a], cwd=mono, capture_output=True, text=True)  # noqa: E731
            r = run("new", "zcheck", "--name", "Z", "--vendor-id", "acme", "--vendor-name", "Acme")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run("upgrade")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((mono / ".claude/skills/urcap/SKILL.md").is_file())
            self.assertEqual(json.loads(run("list").stdout.splitlines()[0])["id"], "zcheck")


if __name__ == "__main__":
    unittest.main()

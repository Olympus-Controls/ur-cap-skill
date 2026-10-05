"""The spec loader, the generator's ownership rules, the monorepo commands and the PSX package."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from urcapgen import cli, generate, psx
from urcapgen.spec import SpecError, parse

BASE = {
    "urcap": {"id": "my-cap", "name": "My Cap", "version": "1.2.3"},
    "vendor": {"id": "acme", "name": "Acme"},
    "program": [
        {
            "id": "grip",
            "title": "Grip",
            "script": "set_force({{force}})\n{{#wait}}sleep(1){{/wait}}\n",
            "label": "{{force}} N",
            "fields": [
                {"key": "force", "type": "float", "default": 20, "min": 0, "max": 140, "unit": "N"},
                {"key": "wait", "type": "bool"},
            ],
        }
    ],
}


def spec_with(**changes):
    data = json.loads(json.dumps(BASE))
    for path, value in changes.items():
        cur = data
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[int(k)] if k.isdigit() else cur[k]
        cur[keys[-1]] = value
    return data


class SpecTests(unittest.TestCase):
    def test_derived_names(self):
        s = parse(BASE, Path("/x"))
        self.assertEqual(s.java_package, "com.acme.mycap")
        self.assertEqual(s.psx_tag(s.programs[0]), "acme-my-cap-grip")
        self.assertEqual(s.ps5_file, "my-cap-ps5-1.2.3.urcap")
        self.assertEqual(s.psx_file, "my-cap-1.2.3.urcapx")
        self.assertEqual(s.programs[0].cls, "Grip")
        self.assertEqual(s.ps5_floor, "5.4")
        self.assertEqual(s.psx_floor, "10.8")

    def test_rejections(self):
        bad = {
            "urcap.id": "My_Cap",
            "urcap.version": "1.2",
            "vendor.java_package": "com.class.x",
            "program.0.script": "{{nope}}",
            "program.0.fields.0.default": 200,
            "program.0.fields.0.type": "vector",
            "program.0.fields.1.key": "force",
            "program.0.id": "Grip",
        }
        for path, value in bad.items():
            with self.assertRaises(SpecError, msg=path):
                parse(spec_with(**{path: value}), Path("/x"))

    def test_toolbar_raises_psx_floor(self):
        s = parse({**BASE, "toolbar": {"title": "T"}}, Path("/x"))
        self.assertEqual(s.psx_floor, "10.10")
        self.assertTrue(s.notes)

    def test_daemon_needs_installation(self):
        with self.assertRaises(SpecError):
            parse({**BASE, "daemon": {}}, Path("/x"))

    def test_choice(self):
        s = parse(
            spec_with(**{"program.0.fields": [{"key": "m", "type": "choice", "options": ["a", {"value": "b", "label": "B"}]}],
                         "program.0.script": "{{m}}", "program.0.label": ""}),
            Path("/x"),
        )  # fmt: skip
        f = s.programs[0].fields[0]
        self.assertEqual(f.default, "a")
        self.assertEqual(f.options[1], {"value": "b", "label": "B"})


# PolyScope 5 API jars for the JDK-backed tests: a directory holding 5.4/ as `urcapgen sdk`
# writes it (CI fetches it once; locally `URCAPGEN_TEST_SDK=<cache>/ps5-sdk`).
_SDK = os.environ.get("URCAPGEN_TEST_SDK", "")


def _link_sdk(root: Path) -> None:
    dest = root / ".urcapgen-cache" / "ps5-sdk"
    if _SDK and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_SDK, dest)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MonorepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "mono"
        code, _, err = run_cli("init", str(self.root))
        self.assertEqual(code, 0, err)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        return run_cli("--repo", str(self.root), *argv)

    def test_layout(self):
        for p in ("urcapgen", ".urcapgen", "tools/urcapgen/cli.py", ".claude/skills/urcap/SKILL.md", "AGENTS.md", ".gitignore"):
            self.assertTrue((self.root / p).exists(), p)
        self.assertFalse((self.root / "tools/urcapgen/__pycache__").exists())

    def test_new_gen_check_and_ownership(self):
        code, out, err = self.cli("new", "demo", "--name", "Demo", "--vendor-id", "acme", "--vendor-name", "Acme",
                                  "--parts", "installation,program,toolbar,daemon")  # fmt: skip
        self.assertEqual(code, 0, err)
        d = self.root / "urcaps" / "demo"
        self.assertEqual(self.cli("gen", "--check")[0], 0)

        # a hook file is yours: edits survive regeneration
        hooks = d / "psx" / "demo-frontend" / "say.hooks.js"
        hooks.write_text(hooks.read_text() + "// mine\n")
        # a generated file is the generator's: edits are reported, then overwritten
        gen_file = d / "psx" / "demo-frontend" / "spec.js"
        gen_file.write_text("// GENERATED by urcapgen\nbroken\n")
        code, _, err = self.cli("gen", "--check")
        self.assertEqual(code, 1)
        self.assertIn("spec.js", err)
        self.assertEqual(self.cli("gen")[0], 0)
        self.assertIn("// mine", hooks.read_text())
        self.assertNotIn("broken", gen_file.read_text())

        # dropping a node deletes its generated files but keeps its hooks
        toml = (d / "urcap.toml").read_text()
        (d / "urcap.toml").write_text(toml.split("# A toolbar button")[0] + "[daemon]\nport = 40405\n")
        self.assertEqual(self.cli("gen")[0], 0)
        self.assertFalse((d / "psx" / "demo-frontend" / "acme-demo-toolbar.js").exists())
        self.assertTrue((d / "psx" / "demo-frontend" / "toolbar.hooks.js").exists())

    def test_parity_js_side(self):
        self.cli("new", "demo", "--name", "Demo", "--vendor-id", "acme", "--vendor-name", "Acme")
        code, out, err = self.cli("parity", "--no-java")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.splitlines()[0])["failures"], 0)

    def test_parity_catches_a_diverging_hook(self):
        self.cli("new", "demo", "--name", "Demo", "--vendor-id", "acme", "--vendor-name", "Acme")
        hooks = self.root / "urcaps" / "demo" / "psx" / "demo-frontend" / "say.hooks.js"
        hooks.write_text(hooks.read_text().replace("return rendered;", 'return rendered + "popup(1)\\n";', 1))
        # without a JDK nothing can compare an edited hook: parity says so instead of passing quietly
        code, out, _ = self.cli("parity", "--no-java")
        self.assertIn("hooks are edited", out)
        from tests.test_scripttpl import _jdk_available

        if not (_jdk_available() and _SDK):
            self.skipTest("the PolyScope 5 side needs a JDK and the 5.4 API jars (URCAPGEN_TEST_SDK)")
        _link_sdk(self.root)
        code, _, err = self.cli("parity")
        self.assertEqual(code, 1)
        self.assertIn("PolyScope X and PolyScope 5 scripts differ", err)

    def test_psx_package_is_reproducible(self):
        self.cli("new", "demo", "--name", "Demo", "--vendor-id", "acme", "--vendor-name", "Acme")
        d = self.root / "urcaps" / "demo"
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            pa = psx.package(d / "psx", a)
            pb = psx.package(d / "psx", b)
            self.assertEqual(pa.read_bytes(), pb.read_bytes())
            with tarfile.open(pa) as t:
                names = t.getnames()
            self.assertEqual(names[0], "manifest.yaml")
            self.assertIn("demo-frontend/contribution.json", names)

    def test_changed_and_release_info(self):
        self.cli("new", "demo", "--name", "Demo", "--vendor-id", "acme", "--vendor-name", "Acme")
        from urcapgen import monorepo

        repo = monorepo.Repo(self.root)
        self.assertEqual(monorepo.touched(repo, ["urcaps/demo/urcap.toml", "README.md"]), ["demo"])
        self.assertEqual(monorepo.touched(repo, ["README.md"]), [])
        self.assertEqual(monorepo.touched(repo, ["tools/urcapgen/cli.py"]), ["demo"])
        self.assertEqual(monorepo.release_info(repo, "demo-v0.1.0")["version"], "0.1.0")
        with self.assertRaises(monorepo.RepoError):
            monorepo.release_info(repo, "demo-v0.2.0")

    def test_upgrade_keeps_user_files(self):
        (self.root / "AGENTS.md").write_text("mine")
        skill = self.root / ".claude/skills/urcap/SKILL.md"
        skill.write_text("stale")
        self.assertEqual(self.cli("upgrade")[0], 0)
        self.assertEqual((self.root / "AGENTS.md").read_text(), "mine")
        self.assertNotEqual(skill.read_text(), "stale")


class GeneratedFilesTests(unittest.TestCase):
    def test_every_generated_file_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = parse({**BASE, "installation": {"fields": []}, "toolbar": {}, "daemon": {}}, Path(tmp))
            for o in generate.files(s):
                if o.owned:
                    continue
                p = Path(tmp) / o.path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(o.text)
                self.assertTrue(generate.is_generated(p), o.path)


if __name__ == "__main__":
    unittest.main()


class SharedPageTests(unittest.TestCase):
    """PolyScope X loads every URCap's presenters into one page: two urcapgen URCaps must not
    clobber each other's spec or runtime."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_two_urcaps_in_one_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = []
            for uid in ("cap-one", "cap-two"):
                data = json.loads(json.dumps(BASE))
                data["urcap"]["id"] = uid
                s = parse(data, Path(tmp) / uid)
                generate.generate(s)
                a = s.root / "psx" / s.psx_archive
                scripts += [a / "urcapgen-runtime.js", a / "spec.js"]
            js = (
                "const vm = require('vm'), fs = require('fs'); globalThis.self = globalThis;"
                + "".join(f"vm.runInThisContext(fs.readFileSync({json.dumps(str(p))}, 'utf8'));" for p in scripts)
                + "process.stdout.write(JSON.stringify({nodes: Object.keys(self.UrcapSpec.nodes).sort(),"
                " urcaps: Object.keys(self.UrcapSpec.urcaps).sort(), runtimes: Object.keys(self.UrcapgenRuntimes)}));"
            )
            r = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
            got = json.loads(r.stdout)
            self.assertEqual(got["nodes"], ["acme-cap-one-grip", "acme-cap-two-grip"])
            self.assertEqual(got["urcaps"], ["acme/cap-one", "acme/cap-two"])
            self.assertEqual(len(got["runtimes"]), 1)

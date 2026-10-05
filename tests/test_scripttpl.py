"""The URScript template language: the reference renderer, and the JS port held to it."""

import json
import random
import shutil
import subprocess
import unittest
from pathlib import Path

from urcapgen import scripttpl as T

RUNTIME = Path(__file__).resolve().parent.parent / "urcapgen" / "runtime" / "urcapgen-runtime.js"


class Literals(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(T.number(1), "1.0")
        self.assertEqual(T.number(0.25), "0.25")
        self.assertEqual(T.number(1 / 3), "0.333333")
        self.assertEqual(T.number(-0.0000001), "0.0")
        self.assertEqual(T.number(0.0078125), "0.007813")  # an exact binary tie: half away from zero
        self.assertEqual(T.number(-0.0078125), "-0.007813")
        self.assertEqual(T.number(123456.0), "123456.0")

    def test_types(self):
        self.assertEqual(T.literal("int", 7), "7")
        self.assertEqual(T.literal("bool", True), "True")
        self.assertEqual(T.literal("string", "a b"), '"a b"')
        self.assertEqual(T.literal("choice", "p[0,0,0,0,0,0]"), "p[0,0,0,0,0,0]")
        self.assertEqual(T.literal("pose", [0.1, 0, 0, 0, 3.14159265, 0]), "p[0.1, 0.0, 0.0, 0.0, 3.141593, 0.0]")
        with self.assertRaises(T.TemplateError):
            T.literal("string", 'say "hi"')


class Render(unittest.TestCase):
    V = {
        "on": ("bool", True),
        "off": ("bool", False),
        "n": ("int", 3),
        "zero": ("float", 0.0),
        "name": ("string", "abc"),
        "empty": ("string", ""),
        "mode": ("choice", "fast"),
    }

    def r(self, t):
        return T.render(t, self.V)

    def test_vars_and_sections(self):
        self.assertEqual(self.r("x = {{n}}"), "x = 3")
        self.assertEqual(self.r("{{#on}}A{{/on}}{{#off}}B{{/off}}{{^off}}C{{/off}}"), "AC")
        self.assertEqual(self.r("{{#zero}}Z{{/zero}}{{^empty}}E{{/empty}}"), "E")
        self.assertEqual(self.r("{{#mode=fast}}F{{/mode}}{{^mode=fast}}S{{/mode}}"), "F")
        self.assertEqual(self.r("{{! a comment }}x"), "x")

    def test_standalone_lines_vanish(self):
        t = "a\n  {{#on}}\n  b\n  {{/on}}\n{{#off}}\nc\n{{/off}}\nd\n"
        self.assertEqual(self.r(t), "a\n  b\nd\n")

    def test_display(self):
        self.assertEqual(T.render("{{name}} {{n}}", self.V, display=True), "abc 3")

    def test_errors(self):
        for bad in ("{{#on}}x", "x{{/on}}", "{{#on}}{{/off}}", "{{Bad}}", "{{missing}}"):
            with self.assertRaises(T.TemplateError, msg=bad):
                self.r(bad)

    def test_names(self):
        self.assertEqual(T.names("{{a}}{{#b=1}}{{c}}{{/b}}{{^d}}{{/d}}"), {"a", "b", "c", "d"})

    def test_lines(self):
        self.assertEqual(T.lines("  a\n\n\tb  \n"), ["a", "b"])


def _random_case(rng: random.Random):
    kinds = {
        "b": ("bool", rng.choice([True, False])),
        "i": ("int", rng.randint(-5, 5)),
        "f": ("float", rng.choice([0.0, 1 / 3, -2.5, 0.0078125, 1e-7, 1234.5678915, rng.uniform(-10, 10)])),
        "s": ("string", rng.choice(["", "x", "a b", "ü"])),
        "c": ("choice", rng.choice(["one", "two", "1.5"])),
        "p": ("pose", [rng.uniform(-1, 1) for _ in range(6)]),
    }
    parts = []
    for _ in range(rng.randint(1, 8)):
        k = rng.choice(list(kinds))
        form = rng.randint(0, 4)
        if form == 0:
            parts.append(f"v={{{{{k}}}}}\n")
        elif form == 1:
            parts.append(f"{{{{#{k}}}}}\nyes {k}\n{{{{/{k}}}}}\n")
        elif form == 2:
            parts.append(f"  {{{{^{k}}}}} no {k} {{{{/{k}}}}}\n")
        elif form == 3 and k == "c":
            parts.append("{{#c=two}}two!{{/c}}\n")
        else:
            parts.append(f"text {k}\n")
    return "".join(parts), kinds


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsPort(unittest.TestCase):
    """urcapgen-runtime.js renders exactly what the reference renders."""

    def test_random_templates(self):
        rng = random.Random(1234)
        cases = [_random_case(rng) for _ in range(400)]
        expected = [T.render(t, v) for t, v in cases]
        expected_display = [T.render(t, v, display=True) for t, v in cases]
        js = (
            f"require({json.dumps(str(RUNTIME))});"
            "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "const U = globalThis.Urcapgen;"
            "const conv = (v) => Object.fromEntries(Object.entries(v).map(([k, [type, value]]) => [k, {type, value}]));"
            "process.stdout.write(JSON.stringify(cases.map(([t, v]) => [U.render(t, conv(v)), U.render(t, conv(v), true)])));"
        )
        r = subprocess.run(["node", "-e", js], input=json.dumps(cases), capture_output=True, text=True, check=True)
        got = json.loads(r.stdout)
        for (t, v), e, ed, (g, gd) in zip(cases, expected, expected_display, got, strict=True):
            self.assertEqual(g, e, f"template {t!r} values {v!r}")
            self.assertEqual(gd, ed, f"display: template {t!r} values {v!r}")

    def test_numbers(self):
        xs = [0.0078125, -0.0078125, 1 / 3, -1e-7, 2.5, 1e-6, 0.0000005, 123.4567895, 99.9999995]
        js = (
            f"require({json.dumps(str(RUNTIME))});"
            "const xs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(xs.map(globalThis.Urcapgen.number)));"
        )
        r = subprocess.run(["node", "-e", js], input=json.dumps(xs), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(r.stdout), [T.number(x) for x in xs])


if __name__ == "__main__":
    unittest.main()


def _jdk_available() -> bool:
    from urcapgen import ps5

    return ps5._jdk_bin() is not None


@unittest.skipUnless(_jdk_available(), "no JDK (URCAPGEN_JDK, JAVA_HOME, javac or Docker)")
class JavaPort(unittest.TestCase):
    """ScriptTemplate.java renders exactly what the reference renders."""

    def test_random_templates(self):
        import tempfile

        from urcapgen import ps5
        from urcapgen.generate import jstr

        rng = random.Random(4321)
        cases = [_random_case(rng) for _ in range(300)]

        def jv(kind, value):
            if kind == "int":
                return f"Integer.valueOf({value})"
            if kind == "float":
                return f"Double.valueOf({float(value)!r})"
            if kind == "bool":
                return "Boolean.TRUE" if value else "Boolean.FALSE"
            if kind == "pose":
                return "new double[] {" + ", ".join(repr(float(x)) for x in value) + "}"
            return jstr(str(value))

        body = []
        for t, v in cases:
            puts = "".join(f"m.put({jstr(k)}, new ScriptTemplate.Value({jstr(kind)}, {jv(kind, val)}));" for k, (kind, val) in v.items())
            body.append(f"m = new HashMap<String, ScriptTemplate.Value>(); {puts} out(ScriptTemplate.render({jstr(t)}, m)); out(ScriptTemplate.render({jstr(t)}, m, true));")
        src = (
            "package t;\nimport java.util.*;\npublic class Main {\n"
            "static void out(String s) { StringBuilder b = new StringBuilder();"
            " for (char c : s.toCharArray()) b.append(String.format(\"%04x\", (int) c)); System.out.println(b); }\n"
            "public static void main(String[] a) { Map<String, ScriptTemplate.Value> m;\n" + "\n".join(body) + "\n}}\n"
        )
        runtime = (RUNTIME.parent / "ScriptTemplate.java").read_text(encoding="utf-8").replace("@@PACKAGE@@", "t")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "t"
            d.mkdir()
            (d / "ScriptTemplate.java").write_text(runtime, encoding="utf-8")
            (d / "Main.java").write_text(src, encoding="utf-8")
            out = Path(tmp) / "classes"
            out.mkdir()
            cc = ps5.java_tool("javac", ["--release", "8", "-nowarn", "-encoding", "UTF-8", "-d", str(out), str(d / "ScriptTemplate.java"), str(d / "Main.java")], [Path(tmp)])
            self.assertEqual(cc.returncode, 0, cc.stderr)
            r = ps5.java_tool("java", ["-cp", str(out), "t.Main"], [Path(tmp)])
            self.assertEqual(r.returncode, 0, r.stderr)
        got = [bytes.fromhex(line).decode("utf-16-be") if line else "" for line in r.stdout.split("\n")[:-1]]
        want = []
        for t, v in cases:
            want += [T.render(t, v), T.render(t, v, display=True)]
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            self.assertEqual(g, w, f"case {i // 2}: {cases[i // 2][0]!r}")

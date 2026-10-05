"""Do both URCaps write the same URScript?

For every node with a script, a set of value cases (the defaults, each limit, each choice,
each bool flipped, awkward floats) is rendered three ways:

* ``reference`` — :mod:`urcapgen.scripttpl` (the template alone; the hooks are code);
* ``psx`` — the generated behavior worker under Node.js, hooks included;
* ``ps5`` — the generated ``<Node>Spec.script`` under a JDK, hooks included.

``psx`` must equal ``ps5`` line for line (indentation and blank lines aside). Where a node's
hooks are still the generated defaults, both must also equal the reference.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ps5, scripttpl
from .generate import jstr
from .spec import Field, Node, Spec

AWKWARD = (0.0078125, -0.0000001, 1 / 3, 123.4567895, -2.5)


def _clamp(f: Field, x: float) -> float:
    if f.min is not None:
        x = max(f.min, x)
    if f.max is not None:
        x = min(f.max, x)
    return int(round(x)) if f.type == "int" else x


def cases(n: Node) -> list[dict]:
    """Value cases for a node: ``[{key: value}]``, the defaults first."""
    base = {f.key: f.default for f in n.fields}
    out = [dict(base)]
    for f in n.fields:
        alts: list = []
        if f.type in ("int", "float"):
            alts += [v for v in (f.min, f.max) if v is not None]
            if f.type == "float":
                alts += [_clamp(f, x) for x in AWKWARD]
            else:
                alts += [_clamp(f, x) for x in (0, 1, 7)]
            alts = [int(a) if f.type == "int" else float(a) for a in alts]
        elif f.type == "bool":
            alts = [not f.default]
        elif f.type == "choice":
            alts = [o["value"] for o in f.options if o["value"] != f.default]
        elif f.type == "string":
            alts = ["", "a b-c_d.e"]
        elif f.type == "pose":
            alts = [[0.1, -0.2, 0.3, 0.0, 3.14159, 0.0], [1 / 3, 0.0078125, -0.0000001, 1.0, -1.0, 0.5]]
        for a in alts:
            c = dict(base)
            c[f.key] = a
            if c not in out:
                out.append(c)
    return out


def _typed(n: Node, values: dict) -> dict:
    return {f.key: (f.type, values[f.key]) for f in n.fields}


def reference(n: Node, values: dict, after: bool = False) -> list[str]:
    return scripttpl.lines(scripttpl.render(n.script_after if after else n.script, _typed(n, values)))


# -- PolyScope X: the worker under node ------------------------------------------------------

_NODE_HARNESS = r"""
const vm = require("vm"), fs = require("fs"), path = require("path");
const [dir, jobsFile] = process.argv.slice(2);
globalThis.self = globalThis;
globalThis.postMessage = () => {};
globalThis.addEventListener = () => {};
globalThis.importScripts = (...names) => {
  for (const n of names) vm.runInThisContext(fs.readFileSync(path.join(dir, n), "utf8"), { filename: n });
};
const jobs = JSON.parse(fs.readFileSync(jobsFile, "utf8"));
const out = [];
for (const j of jobs) {
  if (!self.UrcapScripts || !self.UrcapScripts[j.tag]) importScripts(j.worker);
  const s = self.UrcapScripts[j.tag];
  try {
    const node = { type: j.tag, parameters: j.values };
    out.push({ ok: true, lines: self.Urcapgen.lines(s.script(node, j.after)), problem: s.problem(node) });
  } catch (e) { out.push({ ok: false, error: String(e && e.message || e) }); }
}
process.stdout.write(JSON.stringify(out));
"""


def run_psx(spec: Spec, jobs: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not on PATH (Node.js 18+ runs the PolyScope X workers)")
    archive = spec.root / "psx" / spec.psx_archive
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_NODE_HARNESS, encoding="utf-8")
        jf = Path(tmp) / "jobs.json"
        jf.write_text(json.dumps(jobs), encoding="utf-8")
        r = subprocess.run([node, str(harness), str(archive), str(jf)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


# -- PolyScope 5: the Spec classes under a JDK -------------------------------------------------


def _java_value(f: Field, v) -> str:
    if f.type == "int":
        return f"Integer.valueOf({int(v)})"
    if f.type == "float":
        return f"Double.valueOf({float(v)!r})"
    if f.type == "bool":
        return "Boolean.TRUE" if v else "Boolean.FALSE"
    if f.type == "pose":
        return "new double[] {" + ", ".join(repr(float(x)) for x in v) + "}"
    return jstr(str(v))


def _java_harness(spec: Spec, jobs: list[dict], nodes: dict[str, Node]) -> str:
    body = []
    for j in jobs:
        n = nodes[j["tag"]]
        puts = "".join(
            f"        m.put({jstr(f.key)}, new ScriptTemplate.Value({jstr(f.type)}, {_java_value(f, j['values'][f.key])}));\n"
            for f in n.fields
        )
        call = f"{n.cls}Spec.scriptAfter(m)" if j["after"] else f"{n.cls}Spec.script(m)"
        body.append(
            "        m = new LinkedHashMap<String, ScriptTemplate.Value>();\n"
            + puts
            + f"        emit({call}, {n.cls}Spec.problem(m, null));\n"
        )
    return (
        f"package {spec.java_package};\n\n"
        "import java.util.LinkedHashMap;\nimport java.util.Map;\n\n"
        "public final class UrcapgenParity {\n"
        "    private static final StringBuilder OUT = new StringBuilder(\"[\");\n\n"
        "    private static String q(String s) {\n"
        "        StringBuilder b = new StringBuilder(\"\\\"\");\n"
        "        for (char c : s.toCharArray()) {\n"
        "            if (c == '\"' || c == '\\\\') b.append('\\\\').append(c);\n"
        "            else if (c < 0x20) b.append(String.format(\"\\\\u%04x\", (int) c));\n"
        "            else b.append(c);\n"
        "        }\n"
        "        return b.append('\"').toString();\n    }\n\n"
        "    private static void emit(String script, String problem) {\n"
        "        if (OUT.length() > 1) OUT.append(',');\n"
        "        OUT.append(\"{\\\"ok\\\":true,\\\"lines\\\":[\");\n"
        "        boolean first = true;\n"
        "        for (String l : ScriptTemplate.lines(script)) {\n"
        "            OUT.append(first ? \"\" : \",\").append(q(l));\n            first = false;\n        }\n"
        "        OUT.append(\"],\\\"problem\\\":\").append(problem == null ? \"null\" : q(problem)).append('}');\n    }\n\n"
        "    public static void main(String[] args) {\n"
        "        Map<String, ScriptTemplate.Value> m;\n"
        + "".join(body)
        + "        System.out.print(OUT.append(']'));\n    }\n}\n"
    )


def run_ps5(spec: Spec, jobs: list[dict], sdk_root: Path) -> list[dict]:
    nodes = {spec.psx_tag(n): n for n in spec.nodes}
    src = spec.root / "ps5"
    props = ps5.read_properties((src / "bundle.properties").read_text(encoding="utf-8"))
    plan = ps5.compat_plan(props)
    jars = ps5.sdk_jars(ps5.ensure_sdk(plan["floor"], sdk_root))
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        hdir = t / "src" / spec.java_dir
        hdir.mkdir(parents=True)
        harness = hdir / "UrcapgenParity.java"
        harness.write_text(_java_harness(spec, jobs, nodes), encoding="utf-8")
        classes = t / "classes"
        classes.mkdir()
        sources = ps5.sources_for(src, plan) + [harness]
        # the harness is test code: compile without -Werror's lint (its own boxing etc.)
        cc = ps5.java_tool(
            "javac",
            ["--release", "8", "-nowarn", "-encoding", "UTF-8", "-cp", ":".join(map(str, jars)), "-d", str(classes),
             *map(str, sources)],
            [t, src, *{j.parent for j in jars}],
        )  # fmt: skip
        if cc.returncode != 0:
            raise RuntimeError("javac failed:\n" + (cc.stderr or cc.stdout))
        r = ps5.java_tool(
            "java",
            ["-cp", ":".join([str(classes), *map(str, jars)]), f"{spec.java_package}.UrcapgenParity"],
            [t, *{j.parent for j in jars}],
        )
        if r.returncode != 0:
            raise RuntimeError("java failed:\n" + (r.stderr or r.stdout))
    return json.loads(r.stdout)


# -- the comparison ---------------------------------------------------------------------------


def hooks_untouched(spec: Spec, n: Node) -> bool:
    """Are the node's hook files still what urcapgen created?"""
    from .generate import ps5_hooks, psx_hooks

    pj = spec.root / "ps5" / "src" / spec.java_dir / f"{n.cls}Hooks.java"
    px = spec.root / "psx" / spec.psx_archive / f"{n.id}.hooks.js"
    same = True
    if "ps5" in spec.platforms and pj.exists():
        same &= pj.read_text(encoding="utf-8") == ps5_hooks(spec, n)
    if "psx" in spec.platforms and px.exists():
        same &= px.read_text(encoding="utf-8") == psx_hooks(spec, n)
    return same


def run(spec: Spec, sdk_root: Path, *, java: bool = True) -> dict:
    """``{cases, failures: [...], skipped: [...]}``."""
    jobs: list[dict] = []
    for n in spec.nodes:
        if n.kind == "toolbar":
            continue
        for values in cases(n):
            jobs.append({"tag": spec.psx_tag(n), "worker": f"{spec.psx_tag(n)}.worker.js", "values": values, "after": False})
            if n.allows_children:
                jobs.append({"tag": spec.psx_tag(n), "worker": f"{spec.psx_tag(n)}.worker.js", "values": values, "after": True})
    nodes = {spec.psx_tag(n): n for n in spec.nodes}
    skipped: list[str] = []
    px = run_psx(spec, jobs) if "psx" in spec.platforms else None
    p5 = None
    if java and "ps5" in spec.platforms:
        p5 = run_ps5(spec, jobs, sdk_root)
    elif "ps5" in spec.platforms:
        skipped.append("ps5 (no JDK run requested)")
    failures = []
    untouched = {t: hooks_untouched(spec, n) for t, n in nodes.items()}
    if p5 is None or px is None:
        for t, n in nodes.items():
            if not untouched[t] and n.kind != "toolbar":
                skipped.append(f"{n.id}: its hooks are edited, so only a run with both platforms compares them")
    for i, j in enumerate(jobs):
        n = nodes[j["tag"]]
        where = f"{n.id}{' (after children)' if j['after'] else ''} with {json.dumps(j['values'])}"
        got = {}
        if px is not None:
            got["psx"] = px[i]
        if p5 is not None:
            got["ps5"] = p5[i]
        for k, v in got.items():
            if not v.get("ok"):
                failures.append(f"{where}: {k} raised {v.get('error')}")
        if "psx" in got and "ps5" in got and got["psx"].get("ok") and got["ps5"].get("ok"):
            if got["psx"]["lines"] != got["ps5"]["lines"]:
                failures.append(f"{where}: PolyScope X and PolyScope 5 scripts differ\n  psx: {got['psx']['lines']}\n  ps5: {got['ps5']['lines']}")
            if (got["psx"]["problem"] is None) != (got["ps5"]["problem"] is None):
                failures.append(f"{where}: validators disagree: psx {got['psx']['problem']!r}, ps5 {got['ps5']['problem']!r}")
        if untouched[j["tag"]]:
            ref = reference(n, j["values"], j["after"])
            for k, v in got.items():
                if v.get("ok") and v["lines"] != ref:
                    failures.append(f"{where}: {k} differs from the reference renderer\n  {k}: {v['lines']}\n  ref: {ref}")
    return {"cases": len(jobs), "failures": failures, "skipped": skipped}

"""urcapgen.e2e without a simulator: the version tables, Docker Hub drift, the URScript the
PolyScope 5 run sends, log scanning and the PolyScope X script check. The simulator runs
themselves are `urcapgen e2e-ps5` / `e2e-psx` (Docker; minutes each)."""

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

from urcapgen import e2e
from urcapgen.spec import parse

SPEC = {
    "urcap": {"id": "demo-gripper", "name": "Demo Gripper", "version": "0.1.0"},
    "vendor": {"id": "acme", "name": "Acme"},
    "installation": {
        "script": "global demo_gripper_enabled = {{enabled}}\nglobal demo_gripper_speed = {{speed}}\n",
        "fields": [
            {"key": "enabled", "type": "bool", "default": True},
            {"key": "speed", "type": "float", "default": 0.25, "min": 0.01, "max": 1.0},
        ],
    },
    "program": [
        {
            "id": "say",
            "title": "Say",
            "script": "if demo_gripper_enabled:\n  textmsg({{message}})\n  sleep({{wait}})\nend\n",
            "fields": [
                {"key": "message", "type": "string", "default": "hello"},
                {"key": "wait", "type": "float", "default": 1.0, "min": 0, "max": 60},
            ],
        }
    ],
    "toolbar": {"title": "Demo Gripper"},
    "daemon": {"port": 40405},
}


def spec():
    return parse(json.loads(json.dumps(SPEC)), Path("/nonexistent"))


class Tables(unittest.TestCase):
    def test_ps5_matrix_every_minor_oldest_first(self):
        minors = list(e2e.PS5_MATRIX)
        self.assertEqual(minors[0], "5.4")
        self.assertEqual([int(m.split(".")[1]) for m in minors], list(range(4, 4 + len(minors))))
        for m, (tag, digest) in e2e.PS5_MATRIX.items():
            self.assertTrue(tag == m or tag.startswith(m + "."), tag)
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_psx_releases_sorted_and_pinned(self):
        rel = list(e2e.PSX_RELEASES)
        self.assertEqual(rel, sorted(rel, key=e2e._key))
        self.assertEqual(rel[0], "10.8.0")
        for d in e2e.PSX_RELEASES.values():
            self.assertRegex(d, r"^sha256:[0-9a-f]{64}$")

    def test_versions_from_floor(self):
        self.assertEqual(e2e.ps5_versions("5.24.0")[0], "5.24")
        self.assertEqual(e2e.ps5_versions("5.4"), list(e2e.PS5_MATRIX))
        self.assertNotIn("10.9.0", e2e.psx_versions("10.10"))
        self.assertIn("10.12.0", e2e.psx_versions("10.10"))
        self.assertIn("10.12.1", e2e.psx_versions("10.10"))

    def test_resolve(self):
        tag, image = e2e.resolve_ps5("5.26")
        self.assertEqual(tag, e2e.PS5_MATRIX["5.26"][0])
        self.assertIn("@sha256:", image)
        self.assertEqual(e2e.resolve_ps5("5.12.5"), ("5.12.5", "universalrobots/ursim_e-series:5.12.5"))
        self.assertEqual(e2e.resolve_psx("10.12")[0], "10.12.1")
        self.assertEqual(e2e.resolve_psx("10.12.0")[0], "10.12.0")
        with self.assertRaises(e2e.E2EError):
            e2e.resolve_ps5("5.3")
        with self.assertRaises(e2e.E2EError):
            e2e.resolve_psx("10.99")


class Drift(unittest.TestCase):
    def hub_ps5(self):
        return {tag: digest for tag, digest in e2e.PS5_MATRIX.values()} | {"latest": "x", "5.3": "y"}

    def test_ps5_current(self):
        self.assertEqual(e2e.ps5_drift(self.hub_ps5()), [])

    def test_ps5_new_patch_new_minor_repush_gone(self):
        hub = self.hub_ps5()
        hub["5.26.2"] = "sha256:new"
        hub["5.27.0"] = "sha256:n27"
        hub["5.25.2"] = "sha256:moved"
        del hub["5.24.0"]
        text = "\n".join(e2e.ps5_drift(hub))
        self.assertIn("5.26.2 exists: bump", text)
        self.assertIn("PolyScope 5.27 exists", text)
        self.assertIn("5.25.2 was re-pushed", text)
        self.assertIn("5.24.0 is not on Docker Hub", text)
        self.assertNotIn("5.3", text)

    def test_psx(self):
        hub = dict(e2e.PSX_RELEASES) | {"10.14.0-preview": "x", "0.19.135": "y", "10.7.0": "z"}
        self.assertEqual(e2e.psx_drift(hub), [])
        hub["10.15.0"] = "sha256:n"
        self.assertTrue(any("10.15.0 exists" in p for p in e2e.psx_drift(hub)))


class Ps5Program(unittest.TestCase):
    def test_program(self):
        program, markers = e2e.ps5_program(spec())
        self.assertTrue(program.startswith("def urcapgen_e2e():\n") and program.endswith("end\n"))
        self.assertIn('global demo_gripper_daemon = rpc_factory("xmlrpc", "http://127.0.0.1:40405/RPC2")', program)
        self.assertIn("  global demo_gripper_speed = 0.25\n", program)
        self.assertIn('    textmsg("hello")\n', program)
        self.assertIn("demo_gripper_daemon.ping()", program)
        self.assertEqual(markers[-2:], ["urcapgen_e2e/ping=pong", "urcapgen_e2e/done"])
        # the daemon's preamble comes before the installation script that may use it
        self.assertLess(program.index("rpc_factory"), program.index("demo_gripper_enabled ="))

    def test_services(self):
        self.assertEqual(len(e2e.wanted_services(spec())), 4)

    def test_log_errors(self):
        log = (
            "INFO all fine\n"
            "java.lang.NullPointerException: boom\n"
            "\tat com.acme.demogripper.SayContribution.generateScript(SayContribution.java:40)\n"
            "\tat com.ur.Other.x(Other.java:1)\n"
            "java.lang.IllegalStateException\n"
            "\tat com.ur.polyscope.Something(X.java:2)\n"
        )
        errs = e2e.log_errors(log, spec())
        self.assertEqual(errs[0], "java.lang.NullPointerException: boom")
        self.assertEqual(len(errs), 2)

    def test_felix_ps(self):
        rows = e2e.parse_ps("[  18] [Active     ] [    1] Demo Gripper (0.1.0)\n")
        self.assertEqual(rows, [{"id": 18, "state": "Active", "name": "Demo Gripper (0.1.0)"}])
        listing = "objectClass = [com.ur.urcap.api.contribution.DaemonService]\nobjectClass = com.x.Y\n"
        self.assertEqual(e2e.missing_services(listing, [e2e.SERVICES["daemon"]]), [])


class PsxScript(unittest.TestCase):
    def test_runs(self):
        runs = dict(e2e.expected_psx_runs(spec(), {"speed": 0.5}))
        self.assertEqual(runs["application preamble"][1:], ["global demo_gripper_enabled = True", "global demo_gripper_speed = 0.5"])
        self.assertIn("servicegateway/acme/demo-gripper/demo-gripper-backend/xmlrpc/", runs["application preamble"][0])
        self.assertEqual(runs["program node say"], ["if demo_gripper_enabled:", 'textmsg("hello")', "sleep(1.0)", "end"])

    def test_contains_run(self):
        self.assertTrue(e2e.contains_run(["a", "b", "c"], ["b", "c"]))
        self.assertFalse(e2e.contains_run(["a", "b", "c"], ["a", "c"]))

    def test_new_value(self):
        s = spec()
        f = {f.key: f for f in s.installation.fields}
        self.assertEqual(e2e.new_value(f["enabled"]), ("check", False))
        how, v = e2e.new_value(f["speed"])
        self.assertEqual(how, "fill")
        self.assertTrue(0.01 <= v <= 1.0 and v != 0.25)
        self.assertEqual(e2e.new_value(s.programs[0].fields[0]), ("fill", "helloe2e"))


class Floor(unittest.TestCase):
    def test_skip_below_floor(self):
        s = spec()  # the toolbar raises psx_floor to 10.10
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "r.json"
            args = Namespace(skip_below_floor=True, report=str(report))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertTrue(e2e.below_floor(args, s, "10.8.0", s.psx_floor, "psx_floor"))
            self.assertEqual(json.loads(report.read_text())["skipped"], "below the URCap's floor 10.10")
            self.assertFalse(e2e.below_floor(args, s, "10.10.0", s.psx_floor, "psx_floor"))
            with self.assertRaises(e2e.E2EError):
                e2e.below_floor(Namespace(skip_below_floor=False, report=None), s, "10.9.0", s.psx_floor, "psx_floor")


if __name__ == "__main__":
    unittest.main()

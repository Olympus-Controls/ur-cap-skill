"""The URCap's daemon: one Python XML-RPC server (``<urcap>/daemon/``, yours), run

* on PolyScope 5 by the URCap API's ``DaemonService`` (API 1.3+): the jar carries
  ``daemon/`` as a resource directory; PolyScope extracts it, sets the executable bit and runs
  the entry file under runit (stdout/stderr to svlogd) — so it needs a shebang, LF line
  endings, and must stay in the foreground. URScript reaches it on ``127.0.0.1:<port>``.
  Real e-Series control boxes are only known to have Python 2.7 (UR's own sample daemon is
  Python 2), so the script must run on 2.7 *and* 3.
* on PolyScope X in a backend container (``python:3-alpine``, amd64 — the only architecture a
  robot runs) declared in ``manifest.yaml`` with one ``ingress`` (``proxyUrl: /``). URScript
  reaches it as ``http://servicegateway/<vendor>/<urcap>/<container>/xmlrpc/``; a presenter as
  ``getContainerContributionURL(vendor, urcap, container, "xmlrpc")``.

Either way the installation node's preamble opens ``global <id>_daemon = rpc_factory(...)``
before its own template lines, so every program node calls ``<id>_daemon.<function>(...)``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .spec import Spec

BASE_IMAGE = "python:3.12-alpine"
INGRESS = "xmlrpc"


def global_name(spec: Spec) -> str:
    return f"{spec.id.replace('-', '_')}_daemon"


def ps5_url(spec: Spec) -> str:
    return f"http://127.0.0.1:{spec.daemon.port}/RPC2"


def psx_url(spec: Spec) -> str:
    return f"http://servicegateway/{spec.vendor_id}/{spec.id}/{spec.psx_backend}/{INGRESS}/"


def preamble_line(spec: Spec, url: str) -> str:
    return f'global {global_name(spec)} = rpc_factory("xmlrpc", "{url}")'


def entry_name(spec: Spec) -> str:
    return Path(spec.daemon.entry).name


def daemon_dir(spec: Spec) -> Path:
    return spec.root / Path(spec.daemon.entry).parent


def jar_dir(spec: Spec) -> str:
    """Where ``daemon/`` sits inside the .urcap."""
    return f"{spec.java_dir}/daemon"


# -- PolyScope 5 ----------------------------------------------------------------------------------


def ps5_files(spec: Spec) -> list:
    from .generate import Out, java_header, jstr

    d = spec.java_dir
    text = (
        java_header(spec, "the daemon: PolyScope 5 runs daemon/ from the jar under runit")
        + """
import com.ur.urcap.api.contribution.DaemonContribution;
import com.ur.urcap.api.contribution.DaemonService;
import java.net.MalformedURLException;
import java.net.URL;

public class Daemon implements DaemonService {
"""
        + f"    static final String DIR = {jstr('file:' + jar_dir(spec) + '/')};\n"
        + f"    static final String EXECUTABLE = {jstr('file:' + jar_dir(spec) + '/' + entry_name(spec))};\n"
        + f"    static final String PREAMBLE = {jstr(preamble_line(spec, ps5_url(spec)))};\n"
        + """    private DaemonContribution contribution;

    @Override
    public void init(DaemonContribution c) {
        this.contribution = c;
        try {
            c.installResource(new URL(DIR));
        } catch (MalformedURLException e) {
            throw new IllegalStateException(e);
        }
    }

    @Override
    public URL getExecutable() {
        try {
            return new URL(EXECUTABLE);
        } catch (MalformedURLException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Starts the daemon if it is not running (the installation node calls this). */
    void start() {
        DaemonContribution c = contribution;
        if (c != null && c.getState() != DaemonContribution.State.RUNNING) {
            c.start();
        }
    }

    void stop() {
        DaemonContribution c = contribution;
        if (c != null) {
            c.stop();
        }
    }

    /** RUNNING, STOPPED or ERROR (null before PolyScope initialised the service). */
    DaemonContribution.State state() {
        DaemonContribution c = contribution;
        return c == null ? null : c.getState();
    }
}
"""
    )
    return [Out(f"ps5/src/{d}/Daemon.java", text)]


# -- PolyScope X ----------------------------------------------------------------------------------


def psx_manifest_lines(spec: Spec) -> list[str]:
    b = spec.psx_backend
    return [
        "  containers:",
        f'  - id: "{b}"',
        f'    image: "{b}:latest"',
        "    ingress:",
        f"      - id: {INGRESS}",
        f"        containerPort: {spec.daemon.port}",
        "        protocol: http",
        "        proxyUrl: /",
    ]


def dockerfile(spec: Spec) -> str:
    from .generate import MARK

    e = entry_name(spec)
    return (
        f"# {MARK} from urcap.toml. Do not edit; change the spec (the daemon itself is daemon/).\n"
        f"# The PolyScope X backend: the same daemon PolyScope 5 runs, in a container.\n"
        f"FROM {BASE_IMAGE}\n"
        "WORKDIR /daemon\n"
        "COPY daemon/ /daemon/\n"
        f"EXPOSE {spec.daemon.port}\n"
        f'ENTRYPOINT ["python", "-u", "/daemon/{e}", "--host", "0.0.0.0", "--port", "{spec.daemon.port}"]\n'
    )


def psx_files(spec: Spec) -> list:
    from .generate import Out

    return [Out(f"psx/{spec.psx_backend}/Dockerfile", dockerfile(spec))]


def build_image(spec: Spec, stage: Path) -> Path:
    """``docker build`` (amd64) + ``docker save`` into ``stage/<backend>/<backend>.tar``."""
    if not shutil.which("docker"):
        raise RuntimeError("the PolyScope X backend needs Docker to build its image (or pass --backend-image TAR)")
    b = spec.psx_backend
    tag = f"{b}:latest"
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        shutil.copyfile(spec.root / "psx" / b / "Dockerfile", ctx / "Dockerfile")
        shutil.copytree(daemon_dir(spec), ctx / "daemon", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        r = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", tag, str(ctx)], capture_output=True, text=True, check=False
        )
        if r.returncode != 0:
            raise RuntimeError(f"docker build failed:\n{r.stderr[-3000:]}")
    out = stage / b / f"{b}.tar"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["docker", "save", "-o", str(out), tag], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker save failed: {r.stderr.strip()}")
    return out


def psx_package_hook(spec: Spec, args):
    """The ``extra`` step of :func:`urcapgen.psx.package`: put the backend image in the archive."""
    prebuilt = getattr(args, "backend_image", None)

    def extra(stage: Path) -> None:
        b = spec.psx_backend
        if prebuilt:
            dest = stage / b / f"{b}.tar"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(prebuilt, dest)
        else:
            build_image(spec, stage)

    return extra


# -- shared: the daemon itself (yours) -----------------------------------------------------------


def daemon_py(spec: Spec) -> str:
    g = global_name(spec)
    return f'''#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Yours: urcapgen created this once and never rewrites it.
"""The {spec.name} daemon: an XML-RPC server URScript calls as ``{g}.<function>(...)``.

Runs on PolyScope 5 (the controller's Python — only 2.7 is certain on a real e-Series
control box, so keep this file Python 2.7 *and* 3 compatible: no f-strings, no type hints,
no ``print`` without the __future__ import, stdlib only) and on PolyScope X (Python 3 in the
backend container). Stay in the foreground; stdout/stderr are logged by the platform.

URScript types over XML-RPC: int, float, bool, string, lists of those, and poses
(``p[...]`` arrives as a struct {{"x","y","z","rx","ry","rz"}}). Return one of those too.
"""

from __future__ import print_function

import argparse
import sys

try:  # Python 3
    from socketserver import ThreadingMixIn
    from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer
except ImportError:  # Python 2.7 (PolyScope 5 control box)
    from SimpleXMLRPCServer import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer
    from SocketServer import ThreadingMixIn

PORT = {spec.daemon.port}


class Handler(SimpleXMLRPCRequestHandler):
    # PolyScope 5 calls /RPC2; PolyScope X's ingress strips its prefix down to /
    rpc_paths = ("/", "/RPC2")


class Server(ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
    allow_reuse_address = True


# -- the functions URScript calls ----------------------------------------------------------------


def ping():
    """``{g}.ping()`` → "pong": is the daemon up?"""
    return "pong"


def echo(value):
    """``{g}.echo(x)`` → x."""
    return value


FUNCTIONS = [ping, echo]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="PolyScope 5: 127.0.0.1; the container: 0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    server = Server((args.host, args.port), requestHandler=Handler, allow_none=True, logRequests=False)
    server.register_introspection_functions()
    for fn in FUNCTIONS:
        server.register_function(fn)
    print("{spec.id} daemon on %s:%d" % (args.host, args.port))
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
'''


def shared_files(spec: Spec) -> list:
    from .generate import Out

    return [Out(spec.daemon.entry, daemon_py(spec), owned=True)]

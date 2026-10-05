"""Simulator end-to-end tests: a built URCap in the real PolyScope it is for.

``urcapgen matrix ps5|psx``
    The simulator versions to test, as a JSON list (a CI ``fromJSON`` matrix): PolyScope 5 —
    the newest image of every 5.x minor from the floor (the lowest ``ps5_floor`` in the
    monorepo, or ``--floor``) to the newest; PolyScope X — every release from the floor.
``urcapgen check-tags ps5|psx``
    :data:`PS5_MATRIX` / :data:`PSX_RELEASES` against Docker Hub; exit 1 naming a newer
    minor or patch they lack, a tag Docker Hub no longer serves, or a pinned digest that moved.
``urcapgen e2e-ps5 ID --version 5.x``
    Boots ``universalrobots/ursim_e-series`` (amd64) with ``dist/<id>-ps5-<ver>.urcap`` in
    ``/urcaps`` (the image's entrypoint copies ``/urcaps/*.jar`` into PolyScope's bundle
    directory at start); waits for the Dashboard; asks PolyScope's OSGi framework (the Felix
    remote shell on the container's 127.0.0.1:6666) that the bundle is Active with every node
    service registered, and ``polyscope.log`` for any exception from the URCap's package;
    powers the arm on over the Dashboard; runs the installation preamble plus every program
    node's script (rendered with the defaults by the reference renderer) as one URScript
    program over Primary (30001) and reads its ``textmsg`` markers back; with a daemon, asks
    it for ``ping`` inside the container and from URScript.
``urcapgen e2e-psx ID --version 10.x``
    Boots ``universalrobots/ursim_polyscopex`` (privileged; by digest), installs
    ``dist/<id>-<ver>.urcapx`` over urservice, checks nginx serves the web archive as packaged,
    then clicks through PolyScope X in headless Chromium (Playwright): the application form
    renders, saves and survives a reload; every program node goes from the toolbox into the
    tree and opens its dialog; the program's generated URScript names each node's lines; the
    sidebar item shows; the backend container answers an XML-RPC ``ping``.

Both write a JSON report (``--report``): every check with ok/detail/seconds. Docker comes from
``$DOCKER`` (default ``docker``; ``"sudo docker"`` works). Containers are named
``urcapgen-e2e-<id>-<version>``; a stale one of that name is removed first, and the container
(and its anonymous volumes — the PolyScope X sim's inner Docker is ~10 GB) goes at the end
unless ``--keep``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import scripttpl
from .monorepo import find
from .spec import Spec, load

# -- the version tables ----------------------------------------------------------------------------

PS5_IMAGE = "universalrobots/ursim_e-series"
PSX_IMAGE = "universalrobots/ursim_polyscopex"
HUB_TAGS = "https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100"

# The newest image of every PolyScope 5 minor on Docker Hub, oldest first: {minor: (tag,
# digest)}. 5.4-5.8 carry only the bare minor tag (their VERSION env: 5.4.3, 5.5.1, 5.6.0,
# 5.7.0, 5.8.2). Digests read from Docker Hub 2026-10-05 (every tag of 5.9+ was re-pushed
# 2026-09-17, 5.4-5.8 2026-04-17). `urcapgen check-tags ps5` says when this is behind.
PS5_MATRIX: dict[str, tuple[str, str]] = {
    "5.4": ("5.4", "sha256:a87ffffc68defdc362e3cd6a0f1238e73d116f2947217c21773cb8f3eeea62f6"),
    "5.5": ("5.5", "sha256:4db767c161de847ec4db877bab338b7e32e453d5b45c0ef186fd0de1020aebe5"),
    "5.6": ("5.6", "sha256:0d1f274c5de6ae47089be8df6493979ef1e0bac3c1daead77d2900b03bcf1f34"),
    "5.7": ("5.7", "sha256:c4f74cdf56d894cfb58aaafbf1072dfcf1d0b3702e1154b0e2da7dc4cb5384b3"),
    "5.8": ("5.8", "sha256:26dc0ab1f2c0d3b69cbb4553afd0372e5e22e72762224d46f4f7d72fb4dbf92c"),
    "5.9": ("5.9.4", "sha256:97777d05854a3d258a64a14c6e81cf0f9f1ae120c6fe6b6b2b92287c11e4991b"),
    "5.10": ("5.10.2", "sha256:a4fd16c6bc943660209e13044e352fecd25b74f18c5d86b75a361199d8a52df9"),
    "5.11": ("5.11.11", "sha256:c6cf1d4b91c7b62bf0f8a485d282ee61e0679be2bf0cb97932d9df64f303da79"),
    "5.12": ("5.12.8", "sha256:ee76c9838bb35fc52613e29cc85542022c9e64672087f18401e88e32dc6bfdfb"),
    "5.13": ("5.13.1", "sha256:f8928874060202e34d9e41c758f15cb0745fb14b13399576b100b6623426c06c"),
    "5.14": ("5.14.6", "sha256:fe64ec0b089fa33f70888fb8da22bfba7d11b7ed37388c525f68177836e7fa71"),
    "5.15": ("5.15.2", "sha256:b2bf8936928e553c96bbffa79d66e65e301c80b6605e68f0cc38488c44f0b9ae"),
    "5.16": ("5.16.1", "sha256:7f8b72fbc7c52e9fde434bb015e9f6d01c7a08585f47a782cfe2a71dd1af51f3"),
    "5.17": ("5.17.3", "sha256:a1a260d130c1f1d929b19eb44b5a7346ff15d0768e29c731e7e2936528a48a25"),
    "5.18": ("5.18.1", "sha256:ef06f5f12c9f32f36231c55db035aad45fe02598e5d1cc1e4e163303e791bc14"),
    "5.19": ("5.19.0", "sha256:9d31854e5b181facf37ac1d5a0fcfb03d0e8113cdcec901017b8384a2f98bcbc"),
    "5.20": ("5.20.0", "sha256:1da5ef02b176695e68cd42611950fa061c61b8f83c541b7b98913e316b2f139c"),
    "5.21": ("5.21.3", "sha256:186fe9f0b9994f095b08f1bf28500938abdeb569237b172f04b9065efaf9dc26"),
    "5.22": ("5.22.2", "sha256:fb6e1261fe56420cdd43f22df4ab966c2c439324a215bcdd58486055831c0052"),
    "5.23": ("5.23.0", "sha256:d37c6db55c991c513ce0119ba79267c56f4af477e65c192fbeee33aed4de8c81"),
    "5.24": ("5.24.0", "sha256:3d458c418e113e797f6783658344f43cf200cbb36adbbdde6979e889452b1270"),
    "5.25": ("5.25.2", "sha256:d2a3efbbedb69d3c190144d4cd57c1f67cade009dd48ef46718f05382eeb0893"),
    "5.26": ("5.26.1", "sha256:91fe32e3517b069dfc6b7b72e8f55f2250e29efcdcb22809891b52a48af6e5a9"),
}

# Every PolyScope X release with a simulator image (10.<minor>.<patch>, no -preview/-beta;
# UR also pushes SDK-numbered 0.x tags and 10.x.y-0.n.m aliases of the same digests), oldest
# first: {release: digest}. Multi-arch (amd64 + arm64) index digests from Docker Hub,
# 2026-10-05. PolyScope X 10.8 is
# urcapgen's floor (10.6 / 10.7's own web app does not start on GitHub's amd64 runners).
PSX_RELEASES: dict[str, str] = {
    "10.8.0": "sha256:37430383bcc047e0302fb4ab578cf84a5c8e7e3e281142b28ad59c03a6fb579c",
    "10.9.0": "sha256:9904a17cbaf09c4174127afc8fd00c9123bd3391765ef98c47e61212fa63d400",
    "10.10.0": "sha256:6b8665e173b6dc907bdfaaddd42e92e25aa1da5cc6622dfa888504eeb77e0188",
    "10.11.0": "sha256:49b899317c9cb64a6682f3a3a30ac9fca8a2dbe0d9c3ca7d4588365d54b52c43",
    "10.12.0": "sha256:7bd24159bfa57d5e396163327598129b37a10c50a9298d9ad7af31b2e2e56a11",
    "10.12.1": "sha256:3144ba752b6f8a7d72a05ae317fb4fbfb4258a3260755280e3b2d1090856e557",
    "10.13.0": "sha256:576b0d9d035853bd0bd576e450113199b24b3e8ccb9d649924f1e79ae3367e74",
    "10.14.0": "sha256:f306cd98970aea97d8399cf1fb30c07ddd3713b620ca6c5af9a78a5869ac41ee",
}


class E2EError(RuntimeError):
    pass


def _docker() -> list[str]:
    return (os.environ.get("DOCKER") or "docker").split()


def minor_of(v: str) -> str:
    return ".".join(v.split(".")[:2])


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def ps5_versions(floor: str) -> list[str]:
    """Every minor in :data:`PS5_MATRIX` from ``floor``'s minor up, oldest first."""
    f = _key(minor_of(floor))
    return [m for m in PS5_MATRIX if _key(m) >= f]


def psx_versions(floor: str) -> list[str]:
    """Every release in :data:`PSX_RELEASES` whose minor is ``floor``'s or newer."""
    f = _key(minor_of(floor))
    return [r for r in PSX_RELEASES if _key(minor_of(r)) >= f]


def resolve_ps5(version: str) -> tuple[str, str]:
    """``5.26`` / ``5.26.1`` → ``(tag, image reference)`` (pinned by digest when tabled)."""
    m = minor_of(version)
    if m not in PS5_MATRIX:
        raise E2EError(f"PolyScope {version} is not in urcapgen's PS5_MATRIX ({', '.join(PS5_MATRIX)}); "
                       "`urcapgen matrix ps5` lists the versions")  # fmt: skip
    tag, digest = PS5_MATRIX[m]
    if version in (m, tag):
        return tag, f"{PS5_IMAGE}:{tag}@{digest}"
    return version, f"{PS5_IMAGE}:{version}"  # an older patch of a tabled minor: by tag


def resolve_psx(version: str) -> tuple[str, str]:
    """``10.12`` (its newest patch) / ``10.12.0`` → ``(release, image@digest)``."""
    if version in PSX_RELEASES:
        rel = version
    else:
        same = [r for r in PSX_RELEASES if minor_of(r) == minor_of(version)]
        if not same or version.count(".") > 1:
            raise E2EError(f"PolyScope X {version} is not in urcapgen's PSX_RELEASES ({', '.join(PSX_RELEASES)}); "
                           "`urcapgen matrix psx` lists them")  # fmt: skip
        rel = same[-1]
    return rel, f"{PSX_IMAGE}:{rel}@{PSX_RELEASES[rel]}"


# -- Docker Hub ------------------------------------------------------------------------------------


def fetch_tags(repo: str) -> dict[str, str | None]:
    """``{tag: digest}`` for every tag Docker Hub lists for ``repo``."""
    url: str | None = HUB_TAGS.format(repo=repo)
    out: dict[str, str | None] = {}
    while url:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed https URL)
                page = json.load(r)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise E2EError(f"Docker Hub tags for {repo}: {exc} — check the network and retry") from None
        for t in page.get("results", []):
            out[t["name"]] = t.get("digest")
        url = page.get("next")
    return out


_PS5_TAG = re.compile(r"5\.(\d+)(?:\.(\d+))?")
_PSX_TAG = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def ps5_drift(hub: dict[str, str | None], matrix: dict[str, tuple[str, str]] = PS5_MATRIX) -> list[str]:
    """What PS5_MATRIX is missing given Docker Hub's tags, one sentence per fix."""
    best: dict[int, tuple[tuple[int, int], str]] = {}
    for tag in hub:
        m = _PS5_TAG.fullmatch(tag)
        if not m:
            continue
        minor, patch = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else -1
        if minor not in best or (minor, patch) > best[minor][0]:
            best[minor] = ((minor, patch), tag)
    have = {_key(k)[1]: v for k, v in matrix.items()}
    floor = min(have)
    out = []
    for minor, ((_, patch), tag) in sorted(best.items()):
        if minor < floor:
            continue
        if minor not in have:
            out.append(f"PolyScope 5.{minor} exists ({PS5_IMAGE}:{tag}): add \"5.{minor}\": (\"{tag}\", \"{hub[tag]}\") to PS5_MATRIX in urcapgen/e2e.py")
            continue
        cur_tag, cur_digest = have[minor]
        cm = _PS5_TAG.fullmatch(cur_tag)
        cur_patch = int(cm.group(2)) if cm and cm.group(2) is not None else -1
        if patch > cur_patch:
            out.append(f"PolyScope {tag} exists: bump PS5_MATRIX[\"5.{minor}\"] from {cur_tag} to (\"{tag}\", \"{hub[tag]}\")")
    for _minor, (tag, digest) in have.items():
        if tag not in hub:
            out.append(f"{PS5_IMAGE}:{tag} is not on Docker Hub any more — the matrix cannot pull it")
        elif hub[tag] and hub[tag] != digest:
            out.append(f"{PS5_IMAGE}:{tag} was re-pushed (digest {hub[tag]}, pinned {digest}): re-run its e2e, then update PS5_MATRIX")
    return out  # fmt: skip


def psx_drift(hub: dict[str, str | None], releases: dict[str, str] = PSX_RELEASES) -> list[str]:
    """What PSX_RELEASES is missing given Docker Hub's tags, one sentence per fix."""
    top = max(_key(r) for r in releases)
    out = []
    for tag in sorted((t for t in hub if _PSX_TAG.fullmatch(t) and int(t.split(".")[0]) >= 10), key=_key):
        if tag not in releases and _key(tag) > top:
            out.append(f"PolyScope X {tag} exists: add \"{tag}\": \"{hub[tag]}\" to PSX_RELEASES in urcapgen/e2e.py")
    for tag, digest in releases.items():
        if tag not in hub:
            out.append(f"{PSX_IMAGE}:{tag} is not on Docker Hub any more — the matrix cannot pull it")
        elif hub[tag] and hub[tag] != digest:
            out.append(f"{PSX_IMAGE}:{tag} was re-pushed (digest {hub[tag]}, pinned {digest}): re-run its e2e, then update PSX_RELEASES")
    return out  # fmt: skip


# -- the record --------------------------------------------------------------------------------------


class Checks:
    """Ordered pass/fail checks with timing, printed as they happen."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._t = time.monotonic()

    def _add(self, name: str, ok: bool | None, detail: str) -> None:
        now = time.monotonic()
        self.items.append({"check": name, "ok": ok, "detail": detail, "seconds": round(now - self._t, 1)})
        self._t = now
        mark = {True: "ok  ", False: "FAIL", None: "skip"}[ok]
        print(f"  {mark}  {name}" + (f" — {detail}" if detail else ""), flush=True)

    def start(self) -> None:
        """The next check's clock starts now."""
        self._t = time.monotonic()

    def ok(self, name: str, detail: str = "") -> None:
        self._add(name, True, detail)

    def skip(self, name: str, detail: str) -> None:
        self._add(name, None, detail)

    def fail(self, name: str, detail: str, *, fatal: bool = True) -> None:
        self._add(name, False, detail)
        if fatal:
            raise E2EError(f"{name}: {detail}")

    def expect(self, cond: bool, name: str, detail: str = "", *, fatal: bool = True) -> bool:
        if cond:
            self.ok(name, detail)
        else:
            self.fail(name, detail, fatal=fatal)
        return bool(cond)

    @property
    def passed(self) -> bool:
        return bool(self.items) and all(i["ok"] is not False for i in self.items)


def wait_for(what: str, fn, timeout: float, every: float = 3.0, alive=None, hint: str = ""):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if alive is not None and not alive():
            raise E2EError(f"the simulator container exited while waiting for {what} — see its log in the artifacts")
        if time.monotonic() > deadline:
            raise E2EError(f"timed out after {timeout:.0f} s waiting for {what}" + (f" — {hint}" if hint else ""))
        time.sleep(every)


# -- docker ----------------------------------------------------------------------------------------


def docker(*args: str, timeout: float = 600, check: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
    cmd = [*_docker(), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input, check=False)
    except FileNotFoundError:
        raise E2EError(f"{_docker()[0]} not found — install Docker, or set DOCKER (e.g. DOCKER='sudo docker')") from None
    if check and r.returncode != 0:
        raise E2EError(f"`docker {' '.join(args[:3])} …` failed: {(r.stderr or r.stdout).strip()[-1500:]}")
    return r


def container_name(spec: Spec, version: str) -> str:
    return f"urcapgen-e2e-{spec.id}-{version}"


def remove_container(name: str) -> None:
    # -v: anonymous volumes too (the PolyScope X sim's inner /var/lib/docker is ~10 GB a run)
    docker("rm", "-f", "-v", name, timeout=300)


def running(name: str) -> bool:
    return docker("inspect", "-f", "{{.State.Running}}", name, timeout=30).stdout.strip() == "true"


def host_port(name: str, port: int) -> int:
    out = docker("port", name, f"{port}/tcp", timeout=30, check=True).stdout.split()
    if not out:
        raise E2EError(f"{name} publishes no host port for {port}")
    return int(out[0].rsplit(":", 1)[1])


def container_logs(name: str, tail: int = 400) -> str:
    r = docker("logs", "--tail", str(tail), name, timeout=60)
    return r.stdout + r.stderr


def pull(image: str) -> None:
    """Pull ``image`` unless it is here (a digest-pinned reference checks the bytes)."""
    if docker("image", "inspect", image, timeout=60).returncode == 0:
        return
    print(f"pulling {image} (3-5 GB the first time)", flush=True)
    r = docker("pull", image, timeout=3600)
    if r.returncode != 0:
        raise E2EError(f"docker pull {image} failed: {r.stderr.strip()[-800:]} — check the network / Docker Hub rate limit")


def _artifacts(args) -> Path | None:
    if not getattr(args, "artifacts", None):
        return None
    p = Path(args.artifacts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_report(args, report: dict) -> None:
    if getattr(args, "report", None):
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def below_floor(args, spec: Spec, version: str, floor: str, key: str) -> bool:
    """True (and the skip reported) when ``version`` is below the URCap's floor and
    ``--skip-below-floor`` is given; an error without the flag."""
    if _key(minor_of(version)) >= _key(minor_of(floor)):
        return False
    if not getattr(args, "skip_below_floor", False):
        raise E2EError(f"{version} is below {spec.id}'s compat.{key} {floor} — test {floor} or newer "
                       "(CI passes --skip-below-floor to skip it)")  # fmt: skip
    rec = {"urcap": spec.id, "version": version, "skipped": f"below the URCap's floor {floor}"}
    print(json.dumps(rec), flush=True)
    _write_report(args, rec)
    return True


def _repo_spec(args):
    repo = find(Path(args.repo) if args.repo else Path.cwd())
    return repo, load(repo.urcap_dir(args.id))


# ==================================================================================================
# PolyScope 5
# ==================================================================================================

PS5_ROBOT_MODEL = "UR3"  # the entrypoint picks UR3e where the image has programs.UR3e
SERVICES = {
    "installation": "com.ur.urcap.api.contribution.installation.swing.SwingInstallationNodeService",
    "program": "com.ur.urcap.api.contribution.program.swing.SwingProgramNodeService",
    "toolbar": "com.ur.urcap.api.contribution.toolbar.swing.SwingToolbarService",
    "daemon": "com.ur.urcap.api.contribution.DaemonService",
}
MARK = "urcapgen_e2e"

# polyscope.log never says a URCap *started* (5.24-5.26 only log "Adding 'reference:' to bundle
# uri"); Felix's remote shell does — PolyScope's /ursim/GUI/conf/config.properties enables it on
# 127.0.0.1:6666 inside the container (verified 5.4-5.26).
FELIX_SHELL = r"""
import socket, sys, time
s = socket.create_connection(("127.0.0.1", 6666), 5)
s.settimeout(10)
def until_prompt():
    buf, end = b"", time.time() + 20
    while not buf.rstrip().endswith(b"->") and time.time() < end:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", "replace")
until_prompt()
for c in sys.argv[1:]:
    s.sendall(c.encode() + b"\n")
    print("### " + c)
    print(until_prompt())
"""
_PS_ROW = re.compile(r"^\[\s*(\d+)\]\s*\[\s*([A-Za-z]+)\s*\]\s*\[\s*(\d+)\]\s*(.*?)\s*$")
_FAILURE = re.compile(r"exception|\berror\b|severe|could not|failed|unresolved|refused|rejected|incompatible", re.I)


def felix(name: str, *commands: str) -> dict[str, str]:
    r = docker("exec", "-i", name, "python3", "-", *commands, input=FELIX_SHELL, timeout=90)
    out: dict[str, str] = {}
    for chunk in r.stdout.split("### ")[1:]:
        cmd, _, body = chunk.partition("\n")
        out[cmd.strip()] = body
    return out


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _PS_ROW.match(line.strip())
        if m:
            rows.append({"id": int(m.group(1)), "state": m.group(2), "name": m.group(4)})
    return rows


def wanted_services(spec: Spec) -> list[str]:
    out = []
    if spec.installation:
        out.append(SERVICES["installation"])
    if spec.programs:
        out.append(SERVICES["program"])
    if spec.toolbar:
        out.append(SERVICES["toolbar"])
    if spec.daemon:
        out.append(SERVICES["daemon"])
    return out


def missing_services(listing: str, wanted: list[str]) -> list[str]:
    classes = re.findall(r"objectClass\s*=\s*\[?([\w.$, ]+)\]?", listing)
    named = {c.strip() for group in classes for c in group.split(",")}
    return [w for w in wanted if w not in named]


def log_errors(log: str, spec: Spec) -> list[str]:
    """polyscope.log lines that say the URCap broke: a stack frame in its package (with the
    exception line heading the trace), or a line naming the bundle with a failure word."""
    pkg = spec.java_package
    lines = log.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        text = line.strip()
        if text.startswith("at ") and (pkg + ".") in text:
            head = next((lines[j].strip() for j in range(i - 1, -1, -1) if not lines[j].strip().startswith("at ")), "")
            for t in (head, text):
                if t and t not in out:
                    out.append(t)
        elif (pkg in text or spec.ps5_file in text) and _FAILURE.search(text) and text not in out:
            out.append(text)
    return out


class Dashboard:
    """The Dashboard server (29999): one command per connection, the reply line back."""

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.host, self.port = host, port

    def __call__(self, command: str, timeout: float = 10.0) -> str:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as s:
                s.settimeout(timeout)
                f = s.makefile("rwb")
                f.readline()  # banner
                f.write(command.encode() + b"\n")
                f.flush()
                return f.readline().decode(errors="replace").strip()
        except OSError:
            return ""

    def mode(self) -> str:
        m = re.search(r"Robotmode: ([A-Z_]+)", self("robotmode"))
        return m.group(1) if m else ""

    def ready(self) -> str:
        """Has PolyScope connected to URControl? (NO_CONTROLLER / DISCONNECTED / BOOTING: not
        yet — a ``power on`` then is dropped.)"""
        m = self.mode()
        return m if m and m not in ("NO_CONTROLLER", "DISCONNECTED", "BOOTING") else ""

    def bring_up(self, timeout: float = 180.0) -> str:
        """Power on + brake release → RUNNING (what urctl's bring_up does)."""
        self("close safety popup")
        self("close popup")
        if "PROTECTIVE_STOP" in self("safetymode"):
            self("unlock protective stop")
        self("power on")
        wait_for("the arm to power on (IDLE)", lambda: self.mode() in ("IDLE", "RUNNING"), timeout, 2,
                 hint="Dashboard `power on` was not obeyed; open noVNC (port 6080) to see PolyScope")  # fmt: skip
        self("brake release")
        wait_for("the brakes to release (RUNNING)", lambda: self.mode() == "RUNNING", timeout, 2,
                 hint="Dashboard `brake release` was not obeyed; open noVNC (port 6080) to see PolyScope")  # fmt: skip
        return self.mode()


_ASCII = re.compile(rb"[\x20-\x7e]{4,}")
SCRIPT_ERRORS = re.compile(r"(?i)compile error|syntax error|runtime|exception|not defined|unknown|illegal|invalid|type error|error")


def primary_run(port: int, program: str, stop: str, timeout: float = 120.0, host: str = "127.0.0.1") -> list[str]:
    """Send ``program`` (one ``def … end``) over Primary and read the broadcast — the
    controller interleaves ``textmsg`` and error messages into it — until ``stop`` shows up
    or ``timeout``. Holding the socket open until then is what makes it land reliably
    (verified on URSim 5.4-5.26)."""
    with socket.create_connection((host, port), timeout=10) as s:
        s.sendall(program.encode("utf-8"))
        s.settimeout(0.5)
        buf = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf.extend(chunk)
            if stop.encode() in buf:
                s.settimeout(0.3)
                with contextlib.suppress(OSError):
                    buf.extend(s.recv(65536))
                break
    return [m.group(0).decode("ascii", "replace") for m in _ASCII.finditer(bytes(buf))]


def daemon_global(spec: Spec) -> str:
    return f"{spec.id.replace('-', '_')}_daemon"


def ps5_program(spec: Spec, *, daemon_call: bool = True) -> tuple[str, list[str]]:
    """The URScript a program with the installation node and one of each program node (all
    defaults) runs — the preamble then each node's script, as PolyScope 5 writes it — in one
    ``def``, with ``textmsg`` markers between the parts. Returns ``(program, markers)``."""
    from . import daemon as daemon_mod

    body: list[str] = []
    markers: list[str] = []

    def mark(what: str) -> None:
        markers.append(f"{MARK}/{what}")
        body.append(f'textmsg("{MARK}/{what}")')

    def add(text: str) -> None:
        body.extend(ln.rstrip() for ln in text.splitlines() if ln.strip())

    def values(node) -> dict:
        return {f.key: (f.type, f.default) for f in node.fields}

    mark("start")
    if spec.daemon:
        add(daemon_mod.preamble_line(spec, daemon_mod.ps5_url(spec)))
    if spec.installation:
        add(scripttpl.render(spec.installation.script, values(spec.installation)))
        mark("installation")
    for node in spec.programs:
        add(scripttpl.render(node.script, values(node)))
        if node.script_after:
            add(scripttpl.render(node.script_after, values(node)))
        mark(f"program-{node.id}")
    if spec.daemon and daemon_call:
        body.append(f'textmsg("{MARK}/ping=", {daemon_global(spec)}.ping())')
        markers.append(f"{MARK}/ping=pong")
    mark("done")
    program = f"def {MARK}():\n" + "".join(f"  {ln}\n" for ln in body) + "end\n"
    return program, markers


DAEMON_PING = r"""
import sys
try:
    from xmlrpc.client import ServerProxy
except ImportError:
    from xmlrpclib import ServerProxy
try:
    print(ServerProxy(sys.argv[1]).ping())
except Exception as e:
    print("ERROR %s: %s" % (type(e).__name__, e))
"""

# Run inside the container when a run fails (or always into the artifacts).
PS5_EVIDENCE = r"""
echo '## /ursim/GUI/bundle'; ls -la /ursim/GUI/bundle | grep -v -E ' com\.ur\.|^total' | tail -40
echo '## runit services'; ls -la /home/root/service /etc/service 2>&1 | head -40
for d in /home/root/service/*/; do echo "# $d"; ls -la "$d"; cat "$d/log/main/current" 2>/dev/null | tail -30; done
echo '## daemon processes'; ps -ef | grep -v grep | grep -i -E 'daemon|python' | head -20
echo '## listening'; (ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | head -40
"""


def ps5_collect(name: str, out: Path) -> None:
    for src in ("/ursim/polyscope.log", "/ursim/URControl.log"):
        docker("cp", f"{name}:{src}", str(out / Path(src).name), timeout=120)
    (out / "evidence.txt").write_text(docker("exec", name, "sh", "-c", PS5_EVIDENCE, timeout=120).stdout, encoding="utf-8")
    (out / "container.log").write_text(container_logs(name, 2000), encoding="utf-8")
    shell = felix(name, "ps")
    (out / "felix.txt").write_text("".join(f"### {k}\n{t}" for k, t in shell.items()), encoding="utf-8")


def cmd_e2e_ps5(args) -> int:
    repo, spec = _repo_spec(args)
    if "ps5" not in spec.platforms:
        raise E2EError(f"{spec.id} does not target PolyScope 5 (compat.platforms)")
    jar = repo.dist / spec.ps5_file
    if not jar.is_file():
        raise E2EError(f"{jar} is not built — run `urcapgen build {spec.id} --platform ps5` first")
    tag, image = resolve_ps5(args.version)
    if below_floor(args, spec, tag, spec.ps5_floor, "ps5_floor"):
        return 0
    out = _artifacts(args)
    checks = Checks()
    name = container_name(spec, tag)
    report: dict = {"urcap": spec.id, "version": spec.version, "platform": "ps5", "polyscope": tag,
                    "image": image, "container": name, "checks": checks.items}  # fmt: skip
    t0 = time.monotonic()
    started = False
    try:
        pull(image)
        remove_container(name)
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "urcaps"
            stage.mkdir()
            # the entrypoint copies /urcaps/*.jar: the .urcap is a jar under another suffix
            shutil.copyfile(jar, stage / f"{spec.id}.jar")
            docker("create", "-it", "--name", name, "--label", "urcapgen-e2e=ps5",
                   "--security-opt", "seccomp=unconfined", "--cap-add", "NET_BIND_SERVICE",
                   # URControl mlock()s its pages; under Docker's default memlock (64 MiB on
                   # Docker 29 / WSL2, 2026-10-05) it dies at start: "PThread::start FATAL:
                   # Cannot spawn thread. Error code: 11" — Dashboard stuck at NO_CONTROLLER
                   "--ulimit", "memlock=-1:-1",
                   "-e", f"ROBOT_MODEL={PS5_ROBOT_MODEL}",
                   "-p", "127.0.0.1::29999", "-p", "127.0.0.1::30001", "-p", "127.0.0.1::6080",
                   image, check=True, timeout=300)  # fmt: skip
            started = True
            docker("cp", f"{stage}/.", f"{name}:/urcaps/", check=True, timeout=120)  # the image has an empty /urcaps
        docker("start", name, check=True, timeout=120)
        dash = Dashboard(host_port(name, 29999))
        primary = host_port(name, 30001)
        print(f"started {image} as {name}: Dashboard :{dash.port}, Primary :{primary}, noVNC :{host_port(name, 6080)}", flush=True)
        checks.start()
        mode = wait_for("the Dashboard to report a controller", dash.ready, args.boot_timeout, 3, lambda: running(name),
                        hint="URControl did not come up; see URControl.log in the artifacts")  # fmt: skip
        checks.ok("simulator up", f"PolyScope {tag}: Dashboard robotmode {mode} after {time.monotonic() - t0:.0f} s")

        # -- the bundle -------------------------------------------------------------------------
        want = f"{spec.name} ({spec.version})"
        services = wanted_services(spec)
        seen: dict = {"state": None, "missing": services}

        def bundle_up():
            rows = parse_ps(felix(name, "ps").get("ps", ""))
            row = next((r for r in rows if r["name"] == want), None)
            if not row:
                return False
            seen["state"], seen["id"] = row["state"], row["id"]
            if row["state"] != "Active":
                return False
            cmd = f"inspect service capability {row['id']}"
            seen["missing"] = missing_services(felix(name, cmd).get(cmd, ""), services)
            return not seen["missing"]

        checks.start()
        try:
            wait_for(f"bundle {want} to be Active", bundle_up, args.urcap_timeout, 3, lambda: running(name))
            checks.ok("bundle active", f"{spec.symbolic_name} = {want}, bundle {seen.get('id')}, services: "
                      + ", ".join(s.rsplit(".", 1)[1] for s in services))  # fmt: skip
        except E2EError:
            errs = log_errors(docker("exec", name, "cat", "/ursim/polyscope.log", timeout=60).stdout, spec)
            checks.fail("bundle active",
                        f"{want}: state {seen['state'] or 'not installed'}, missing services "
                        f"{', '.join(seen['missing']) or 'none'} after {args.urcap_timeout:.0f} s"
                        + (f"; polyscope.log: {' | '.join(errs[:6])}" if errs else "")
                        + " — fix the exception named (it is in the generated or hook Java), rebuild, re-run")  # fmt: skip
        errs = log_errors(docker("exec", name, "cat", "/ursim/polyscope.log", timeout=60).stdout, spec)
        checks.expect(not errs, "no exceptions from the URCap at start",
                      " | ".join(errs[:8]) + " — the stack names the class; fix it and rebuild" if errs else f"polyscope.log names no {spec.java_package} failure",
                      fatal=False)  # fmt: skip

        # -- the daemon (DaemonService: started by the installation node) -----------------------
        if spec.daemon:
            url = f"http://127.0.0.1:{spec.daemon.port}/RPC2"
            last = {"reply": ""}

            def pong():
                r = docker("exec", "-i", name, "python3", "-", url, input=DAEMON_PING, timeout=60)
                last["reply"] = (r.stdout + r.stderr).strip()
                return last["reply"] == "pong"

            checks.start()
            try:
                wait_for("the daemon to answer ping", pong, args.urcap_timeout, 3, lambda: running(name))
                checks.ok("daemon answers", f"ping on {url} inside the container → pong (started by the installation node with the default installation)")
            except E2EError:
                checks.fail("daemon answers",
                            f"ping on {url} inside the container: {last['reply'][-300:] or 'no reply'} — "
                            "see evidence.txt (runit service + its log) in the artifacts; the daemon must run on "
                            "Python 2.7 and stay in the foreground", fatal=False)  # fmt: skip

        # -- the URScript -----------------------------------------------------------------------
        checks.start()
        final = dash.bring_up()
        checks.ok("arm powered on", f"robotmode {final}")
        program, markers = ps5_program(spec)
        if out:
            (out / "program.script").write_text(program, encoding="utf-8")
        checks.start()
        captured = primary_run(primary, program, f"{MARK}/done", timeout=args.script_timeout)
        flat = [c.replace(" ", "") for c in captured]
        got = [m for m in markers if any(m.replace(" ", "") in c for c in flat)]
        errors = [c for c in captured if SCRIPT_ERRORS.search(c) and MARK not in c]
        if out:
            (out / "primary.txt").write_text("\n".join(c for c in captured if not c.isspace()), encoding="utf-8")
        ok = len(got) == len(markers) and not errors
        missing = [m for m in markers if m not in got]
        checks.expect(ok, "URScript runs",
                      f"{len(markers)} markers ({', '.join(m.split('/', 1)[1] for m in markers)})" if ok else
                      (f"missing {', '.join(missing)}" if missing else "all markers")
                      + (f"; controller said: {' | '.join(errors[:6])[:600]}" if errors else "")
                      + " — the program is program.script in the artifacts; the first missing marker follows the failing part",
                      fatal=False)  # fmt: skip
        errs = log_errors(docker("exec", name, "cat", "/ursim/polyscope.log", timeout=60).stdout, spec)
        checks.expect(not errs, "no exceptions from the URCap at the end",
                      " | ".join(errs[:8]) if errs else "none", fatal=False)  # fmt: skip
    except E2EError as exc:
        if not checks.items or checks.items[-1]["ok"] is not False:
            checks.fail("run", str(exc), fatal=False)
    except Exception as exc:  # noqa: BLE001 — every failure is a reported check
        checks.fail("run", f"{type(exc).__name__}: {exc}", fatal=False)
    finally:
        if started:
            if out and (not checks.passed or args.keep or os.environ.get("URCAPGEN_E2E_COLLECT", "1") == "1"):
                with contextlib.suppress(Exception):
                    ps5_collect(name, out)
            if args.keep:
                print(f"keeping {name}: Dashboard :{host_port(name, 29999)}, noVNC http://127.0.0.1:{host_port(name, 6080)}/vnc.html "
                      f"(docker rm -f -v {name})", flush=True)  # fmt: skip
            else:
                remove_container(name)
    report["passed"] = checks.passed
    report["seconds"] = round(time.monotonic() - t0, 1)
    _write_report(args, report)
    print(f"e2e-ps5: {'passed' if checks.passed else 'FAILED'} — {spec.id} {spec.version} on PolyScope {tag}", flush=True)
    return 0 if checks.passed else 1


# ==================================================================================================
# PolyScope X
# ==================================================================================================

# The simulator's own "ready": its web-bootstrapper installs UR's URCaps after the web UI is
# already answering, then logs this; installing ours before it raced that last pass
# ("An error occurred while starting the application"; seen 2026-09-29).
BOOTSTRAP_DONE = "Done, time to sleep forever"
BOOTSTRAP_STATUS = "/universal-robots/bootstrapper/status"
URSERVICE = "/universal-robots/urservice/api/v1/urcaps"
# Dialogs the sim raises when its web UI comes up before the controller: acknowledged, then
# the page reloaded; any other error dialog fails the run with its text.
BOOT_RACE_DIALOGS = ("No kinematics info available", "Internal software communication error")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, timeout: float = 10, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (the local sim)
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError):
        return 0, b""


def bootstrap_finished(port: int, name: str) -> bool:
    if BOOTSTRAP_DONE in container_logs(name, 100_000):
        return True
    status, body = http(f"http://127.0.0.1:{port}{BOOTSTRAP_STATUS}", timeout=5)
    if status != 200:
        return False
    try:
        d = json.loads(body)
    except ValueError:
        return False
    return bool(d.get("isFinished")) and not d.get("inProgress") and not d.get("errorExitMessage")


def archive_mismatches(url: str, packed: dict[str, bytes]) -> list[str]:
    """``"<file>: <what>"`` for every packaged file nginx does not serve byte for byte. Not
    "one file answers 200": urservice writes an nginx conf and SIGHUPs nginx, and until the
    old workers go a request lands on either generation (seen 2026-10-01)."""
    out = []
    for rel, body in packed.items():
        status, got = http(url + rel)
        if status != 200:
            out.append(f"{rel}: HTTP {status}")
        elif got != body:
            out.append(f"{rel}: {len(got)} bytes, not the {len(body)} packaged")
    return out


def xmlrpc_ping_body() -> bytes:
    return b"<?xml version='1.0'?><methodCall><methodName>ping</methodName><params></params></methodCall>"


def backend_path(spec: Spec) -> str:
    """What ``getContainerContributionURL(vendor, urcap, container, "xmlrpc")`` builds, after
    the origin."""
    from . import daemon as daemon_mod

    return f"/{spec.vendor_id}/{spec.id}/{spec.psx_backend}/{daemon_mod.INGRESS}/"


def new_value(f) -> tuple[str, object]:
    """A value for field ``f`` other than its default: ``(how, value)`` — how is fill, check
    or select."""
    if f.type == "bool":
        return "check", not f.default
    if f.type == "choice":
        other = next((o["value"] for o in f.options if o["value"] != f.default), f.default)
        return "select", other
    if f.type == "string":
        return "fill", (f.default + "e2e") if f.default else "e2e"
    if f.type in ("int", "float"):
        lo = f.min if f.min is not None else -1e9
        hi = f.max if f.max is not None else 1e9
        cand = [f.default + 1, f.default - 1, (lo + hi) / 2] if f.type == "int" else [
            round(f.default * 2, 3) if f.default else 0.5, round(f.default / 2, 3), (lo + hi) / 2]
        for c in cand:
            c = int(c) if f.type == "int" else c
            if lo <= c <= hi and c != f.default:
                return "fill", c
    return "", None


FORM_INPUTS = ".ucg .grid > input, .ucg .grid > select, .ucg .grid > div.pose"


def _shots(page, out: Path | None):
    def shot(name: str) -> None:
        if out:
            with contextlib.suppress(Exception):
                page.screenshot(path=str(out / f"{name}.png"))

    return shot


def clear_boot_dialogs(page) -> list[str]:
    seen = []
    for _ in range(10):
        if not page.get_by_text("An error occurred").first.is_visible():
            return seen
        known = next((t for t in BOOT_RACE_DIALOGS if page.get_by_text(t).first.is_visible()), None)
        if known is None:
            body = page.locator("body").inner_text()
            at = body.find("An error occurred")
            raise E2EError(f"PolyScope error dialog: {body[at : at + 200]!r}")
        seen.append(known)
        button = page.get_by_role("button", name=re.compile(r"^(ok|retry|try again|close)$", re.I)).first
        if button.is_visible():
            button.click(timeout=10_000)
        else:
            page.reload(wait_until="networkidle", timeout=180_000)
        page.wait_for_timeout(8000)
    raise E2EError(f"PolyScope keeps raising {seen[-1]!r}")


def load_ui(page, origin: str) -> None:
    nav = page.get_by_text("Application", exact=True).first
    for _ in range(4):  # the UI's first load after boot can come up partial
        page.goto(origin + "/", wait_until="networkidle", timeout=180_000)
        with contextlib.suppress(Exception):
            nav.wait_for(state="visible", timeout=60_000)
        page.wait_for_timeout(2000)
        if clear_boot_dialogs(page):
            page.wait_for_timeout(5000)
            continue
        if nav.is_visible():
            return
    raise E2EError("PolyScope X's web UI never showed its Application tab — see last.png in the artifacts")


def open_toolbox(page) -> None:
    """The + under the program's first row opens the toolbox (the small
    call-to-action icon button on 10.13 / 10.14; older releases: the first button drawn where
    that + sits)."""
    cta = page.locator("button.ur-icon-button-small.ur-icon-button-cta")
    if cta.count():
        cta.first.click(timeout=30_000)
        return
    buttons = page.locator("button")
    for i in range(buttons.count()):
        box = buttons.nth(i).bounding_box()
        if box and 100 < box["x"] < 300 and 90 < box["y"] < 200 and box["width"] < 60:
            buttons.nth(i).click(timeout=30_000)
            return
    raise E2EError("no toolbox + button under the program — see the screenshot; PolyScope X's program screen changed")


def browser_checks(checks: Checks, spec: Spec, port: int, out: Path | None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        checks.fail("playwright", "not importable — `python3 -m pip install playwright==1.63.0 && "
                    "python3 -m playwright install --with-deps chromium`")  # fmt: skip
        return
    origin = f"http://127.0.0.1:{port}"
    ours = f"/{spec.vendor_id}/{spec.id}/"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors: list[str] = []
        console: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"{e}\n{getattr(e, 'stack', '') or ''}"))
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        shot = _shots(page, out)
        try:
            checks.start()
            load_ui(page, origin)
            checks.ok("PolyScope X UI loads", origin)
            inst_values = application_checks(checks, spec, page, origin, shot) if spec.installation else {}
            for node in spec.programs:
                program_node_checks(checks, spec, node, page, origin, shot)
            if spec.programs or spec.installation:
                script_checks(checks, spec, page, out, inst_values)
            if spec.toolbar:
                sidebar_checks(checks, spec, page, origin, shot)
            if spec.daemon:
                backend_page_check(checks, spec, page)
            mine = [e for e in errors if ours in e or spec.psx_tag() in e or "Urcapgen" in e or "UrcapSpec" in e]
            checks.expect(not mine, "no page errors from the URCap",
                          "; ".join(mine)[:600] + " — fix the generator / hooks JS named in the stack" if mine else "none",
                          fatal=False)  # fmt: skip
        finally:
            shot("last")
            if out:
                (out / "console.txt").write_text("\n".join(console + ["--- page errors ---", *errors]), encoding="utf-8")
            browser.close()


def application_checks(checks: Checks, spec: Spec, page, origin: str, shot) -> dict:
    """(a) the application node; returns the field values it changed (for the script check)."""
    node = spec.installation
    tag = spec.psx_tag(node)

    def open_node() -> None:
        page.get_by_text("Application", exact=True).first.click(timeout=30_000)
        click_text(page, node.title)
        page.wait_for_selector(tag, state="attached", timeout=60_000)
        page.wait_for_function(f"() => document.querySelectorAll('{tag} .ucg .grid > *').length > 0 || {len(node.fields)} === 0",
                               timeout=30_000)  # fmt: skip

    checks.start()
    try:
        open_node()
    except Exception as exc:  # noqa: BLE001
        shot("application-missing")
        checks.fail("application node renders",
                    f"<{tag}> did not appear under Application → {node.title!r}: {str(exc).splitlines()[0][:200]} — "
                    "see application-missing.png; check contribution.json applicationNodes / the presenter", fatal=False)  # fmt: skip
        return {}
    n = page.locator(f"{tag} >> css={FORM_INPUTS}").count()
    checks.expect(n == len(node.fields), "application node renders",
                  f"<{tag}> with {n} inputs for {len(node.fields)} fields", fatal=False)  # fmt: skip
    shot("application")
    field = next((f for f in node.fields if new_value(f)[0]), None)
    if field is None:
        checks.skip("application value persists", "no field to change")
        return {}
    how, value = new_value(field)
    idx = node.fields.index(field)
    el = page.locator(f"{tag} >> css={FORM_INPUTS}").nth(idx)
    checks.start()
    if how == "check":
        el.click()
    elif how == "select":
        el.select_option(str(value))
    else:
        el.fill(str(value))
        el.press("Tab")
    page.wait_for_timeout(2500)  # updateNode is async
    load_ui(page, origin)
    open_node()
    el = page.locator(f"{tag} >> css={FORM_INPUTS}").nth(idx)
    page.wait_for_timeout(1500)
    got = el.is_checked() if how == "check" else el.input_value()
    same = got == value if how == "check" else (str(got) == str(value) or _num_eq(got, value))
    shot("application-reloaded")
    checks.expect(same, "application value persists",
                  f"{field.key}: set {value!r}, after a reload {got!r}" +
                  ("" if same else " — applicationNodeService.updateNode did not keep it (urcapgen-runtime.js form/save)"),
                  fatal=False)  # fmt: skip
    return {field.key: value} if same else {}


def _num_eq(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def program_node_checks(checks: Checks, spec: Spec, node, page, origin: str, shot) -> None:
    tag = spec.psx_tag(node)
    dialog = f"{tag}-dialog"
    checks.start()
    try:
        page.get_by_text("Program", exact=True).first.click(timeout=30_000)
        page.wait_for_timeout(2000)
        open_toolbox(page)
        page.wait_for_timeout(1000)
        shot(f"toolbox-{node.id}")
        click_text(page, node.title, timeout=30_000)
        page.wait_for_selector(tag, state="attached", timeout=60_000)
        page.wait_for_function(f"() => !!document.querySelector('{tag} button')", timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        shot(f"program-{node.id}-missing")
        checks.fail(f"program node {node.id} inserts",
                    f"{node.title!r} from the toolbox did not put <{tag}> in the tree: {str(exc).splitlines()[0][:200]} — "
                    f"see toolbox-{node.id}.png / program-{node.id}-missing.png", fatal=False)  # fmt: skip
        return
    row = page.locator(tag).first
    checks.ok(f"program node {node.id} inserts", f"tree row: {row.inner_text()[:120]!r}")
    shot(f"program-{node.id}-row")
    checks.start()
    try:
        row.locator("button").first.click(force=True)
        page.wait_for_selector(dialog, state="attached", timeout=30_000)
        page.wait_for_function(f"() => document.querySelectorAll('{dialog} .ucg .grid > *').length > 0 || {len(node.fields)} === 0",
                               timeout=30_000)  # fmt: skip
        n = page.locator(f"{dialog} >> css={FORM_INPUTS}").count()
        shot(f"program-{node.id}-dialog")
        checks.expect(n == len(node.fields), f"program node {node.id} dialog",
                      f"<{dialog}> with {n} inputs for {len(node.fields)} fields", fatal=False)  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        shot(f"program-{node.id}-dialog-missing")
        checks.fail(f"program node {node.id} dialog",
                    f"the row's Edit did not open <{dialog}>: {str(exc).splitlines()[0][:200]} — "
                    "dialogService.openCustomDialog / customElements for the dialog tag", fatal=False)  # fmt: skip
    with contextlib.suppress(Exception):
        page.get_by_role("button", name=re.compile(r"^(done|ok|close)$", re.I)).first.click(timeout=10_000)
        page.wait_for_selector(dialog, state="detached", timeout=15_000)


# PolyScope X's own "View script" (☰ → System Manager → Programs → View script) GETs this:
# {"text": base64(the URScript the open program generates)}, no auth needed (10.13.0,
# 2026-10-05). Application 0 / program 0 is the sim's default program, the one the program
# node checks inserted into.
PROGRAM_SCRIPT = "/universal-robots/polyscopex-services/polyscopex-services/rest-api/program/execution/0/0/DRAFT"


def contains_run(haystack: list[str], needle: list[str]) -> bool:
    """Is ``needle`` a contiguous run of ``haystack``?"""
    if not needle:
        return True
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def expected_psx_runs(spec: Spec, inst_values: dict | None) -> list[tuple[str, list[str]]]:
    """``[(what, lines)]`` the program's script must hold, each as a contiguous run: the
    installation preamble (the daemon's rpc_factory line, then its template with the values the
    application check left) and each program node's script with its defaults."""
    from . import daemon as daemon_mod

    runs = []
    if spec.installation:
        vals = {f.key: (f.type, (inst_values or {}).get(f.key, f.default)) for f in spec.installation.fields}
        lines = scripttpl.lines(scripttpl.render(spec.installation.script, vals))
        if spec.daemon:
            lines = [daemon_mod.preamble_line(spec, daemon_mod.psx_url(spec)), *lines]
        runs.append(("application preamble", lines))
    for node in spec.programs:
        vals = {f.key: (f.type, f.default) for f in node.fields}
        runs.append((f"program node {node.id}", scripttpl.lines(scripttpl.render(node.script, vals))))
        if node.script_after:
            runs.append((f"program node {node.id} (after)", scripttpl.lines(scripttpl.render(node.script_after, vals))))
    return [(w, ln) for w, ln in runs if ln]


def script_checks(checks: Checks, spec: Spec, page, out: Path | None, inst_values: dict | None) -> None:
    """(c) the URScript PolyScope X generates for the program the node checks built."""
    import base64

    checks.start()
    js = """async (path) => { const r = await fetch(path); return { status: r.status, body: await r.text() }; }"""
    res = page.evaluate(js, PROGRAM_SCRIPT)
    try:
        text = base64.b64decode(json.loads(res["body"])["text"]).decode("utf-8", "replace")
    except (ValueError, KeyError, TypeError):
        checks.fail("program URScript", f"GET {PROGRAM_SCRIPT} → HTTP {res.get('status')} {res.get('body', '')[:200]!r} — "
                    "PolyScope X's View script endpoint changed on this release; open ☰ → Programs → View script by hand",
                    fatal=False)  # fmt: skip
        return
    if out:
        (out / "program.script").write_text(text, encoding="utf-8")
    have = scripttpl.lines(text)
    runs = expected_psx_runs(spec, inst_values)
    missing = [w for w, ln in runs if not contains_run(have, ln)]
    # (the `$ n "…"` line markers name the node type on 10.13, its label on 10.10: not checked)
    ok = not missing
    checks.expect(ok, "program URScript",
                  f"{len(have)} lines hold " + ", ".join(w for w, _ in runs) if ok else
                  f"missing {', '.join(missing)} — compare program.script in the artifacts with "
                  "`urcapgen render`; the worker's generatePreamble / generateCodeBeforeChildren wrote something else",
                  fatal=False)  # fmt: skip


def click_text(page, text: str, *, outside_sidebar: bool = True, timeout: float = 60_000) -> None:
    """Click the first visible element whose text is exactly ``text`` — not the sidebar's
    button of the same name (a toolbar titled like the URCap labels its sidebar button so)."""
    deadline = time.monotonic() + timeout / 1000
    js = """([text, outside]) => {
      document.querySelectorAll('[data-urcapgen-e2e]').forEach((e) => e.removeAttribute('data-urcapgen-e2e'));
      const all = Array.from(document.querySelectorAll('body *')).filter((e) =>
        (e.textContent || '').trim() === text && !Array.from(e.children).some((c) => (c.textContent || '').trim() === text));
      const ok = all.filter((e) => e.getClientRects().length
        && !(outside && e.closest('button') && e.closest('button').querySelector('.ur-icon-button-label')));
      if (!ok.length) return false;
      ok[0].setAttribute('data-urcapgen-e2e', 'target');
      return true;
    }"""
    while not page.evaluate(js, [text, outside_sidebar]):
        if time.monotonic() > deadline:
            raise E2EError(f"no element reading {text!r} on the page")
        page.wait_for_timeout(1000)
    page.locator("[data-urcapgen-e2e=target]").first.click(timeout=30_000)


def sidebar_checks(checks: Checks, spec: Spec, page, origin: str, shot) -> None:
    """(d) the sidebar item: its button in PolyScope X's right-hand rail (an icon over the
    title) opens a panel holding the item's element."""
    tag = spec.psx_tag(spec.toolbar)
    title = spec.toolbar.title
    checks.start()
    button = page.locator("button", has=page.locator("span.ur-icon-button-label", has_text=title))
    try:
        button.first.wait_for(state="visible", timeout=30_000)
    except Exception:  # noqa: BLE001
        shot("sidebar-missing")
        checks.fail("sidebar item shows", f"no sidebar button labelled {title!r} — see sidebar-missing.png; check "
                    "contribution.json sidebarItems and the i18n sidebar-items title", fatal=False)  # fmt: skip
        return
    button.first.click(force=True)  # an overlay backdrop can linger over the rail just after load
    try:
        page.wait_for_selector(tag, state="attached", timeout=30_000)
        page.wait_for_function(f"() => !!document.querySelector('{tag} .ucg')", timeout=30_000)
        shot("sidebar")
        checks.ok("sidebar item shows", f"button {title!r} opens <{tag}>: {page.locator(tag).first.inner_text()[:100]!r}")
    except Exception as exc:  # noqa: BLE001
        shot("sidebar")
        checks.fail("sidebar item shows", f"button {title!r} did not render <{tag}>: {str(exc).splitlines()[0][:200]} — "
                    "see sidebar.png; the sidebar presenter (customElements) or its hooks", fatal=False)  # fmt: skip
    with contextlib.suppress(Exception):
        button.first.click(force=True)  # close the panel again


def backend_page_check(checks: Checks, spec: Spec, page) -> None:
    path = backend_path(spec)
    checks.start()
    js = """async (path) => {
      const url = `${location.protocol}//${location.host}${path}`;
      try {
        const r = await fetch(url, { method: "POST", headers: { "Content-Type": "text/xml" }, body: %s });
        return { url, status: r.status, body: (await r.text()).slice(0, 600) };
      } catch (e) { return { url, error: String(e) }; }
    }""" % json.dumps(xmlrpc_ping_body().decode())  # noqa: UP031 — JS full of ${...}
    res: dict = {}
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        res = page.evaluate(js, path)
        if res.get("status") == 200 and "pong" in res.get("body", ""):
            break
        page.wait_for_timeout(5000)
    ok = res.get("status") == 200 and "<string>pong</string>" in res.get("body", "")
    checks.expect(ok, "backend answers ping from the page",
                  f"POST {res.get('url')} → HTTP {res.get('status')} {res.get('error') or res.get('body', '')[:200]!r}"
                  + ("" if ok else " — check the backend container's log (`docker exec <sim> docker ps -a / logs`) "
                     "and manifest.yaml's ingress"), fatal=False)  # fmt: skip


PSX_EVIDENCE = r"""
echo '## inner docker ps'; docker ps -a 2>&1 | head -40
for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -i -E '%s' ); do echo "## logs $c"; docker logs --tail 80 "$c" 2>&1; done
"""


def cmd_e2e_psx(args) -> int:
    repo, spec = _repo_spec(args)
    if "psx" not in spec.platforms:
        raise E2EError(f"{spec.id} does not target PolyScope X (compat.platforms)")
    package = repo.dist / spec.psx_file
    if not package.is_file():
        raise E2EError(f"{package} is not built — run `urcapgen build {spec.id} --platform psx` first")
    rel, image = resolve_psx(args.version)
    if below_floor(args, spec, rel, spec.psx_floor, "psx_floor"):
        return 0
    out = _artifacts(args)
    checks = Checks()
    name = container_name(spec, rel)
    port = args.port or free_port()
    report: dict = {"urcap": spec.id, "version": spec.version, "platform": "psx", "polyscope": rel,
                    "image": image, "container": name, "port": port, "checks": checks.items}  # fmt: skip
    t0 = time.monotonic()
    started = False
    try:
        pull(image)
        remove_container(name)
        arch = "arm64" if os.uname().machine.lower() in ("arm64", "aarch64") else "amd64"
        docker("run", "-d", "--name", name, "--label", "urcapgen-e2e=psx", "--privileged",
               "-e", f"HOST_ARCH={arch}", "-e", "ROBOT_TYPE=UR10",
               "-p", f"127.0.0.1:{port}:80", "--add-host", "host.docker.internal:host-gateway",
               image, check=True, timeout=300)  # fmt: skip
        started = True
        print(f"started {image} as {name} on http://127.0.0.1:{port}", flush=True)
        base = f"http://127.0.0.1:{port}"
        alive = lambda: running(name)  # noqa: E731
        checks.start()
        wait_for("the urservice URCap endpoint", lambda: http(base + URSERVICE)[0] == 200, args.boot_timeout, 5, alive)
        wait_for("the PolyScope X web UI", lambda: http(base + "/")[0] == 200, args.boot_timeout, 5, alive)
        wait_for("the simulator's bootstrapper to finish", lambda: bootstrap_finished(port, name), args.boot_timeout, 3, alive)
        checks.ok("simulator up", f"PolyScope X {rel} ready after {time.monotonic() - t0:.0f} s")

        from . import psx

        checks.start()
        res = psx.install(package, "127.0.0.1", port, replace=True)
        checks.expect(res["ok"], "install accepted", f"HTTP {res['status']} {res.get('hint', '')}".strip())
        listed = [it for it in psx.list_urcaps("127.0.0.1", port)
                  if (it.get("id") or {}).get("urcapID") == spec.id and (it.get("id") or {}).get("vendorID") == spec.vendor_id]  # fmt: skip
        checks.expect(bool(listed) and listed[0].get("version") == spec.version, "listed",
                      f"{spec.vendor_id}/{spec.id} {listed[0].get('version') if listed else 'not listed'}")  # fmt: skip
        with tarfile.open(package, "r:gz") as tar:
            prefix = f"{spec.psx_archive}/"
            packed = {m.name[len(prefix):]: tar.extractfile(m).read()
                      for m in tar.getmembers() if m.isfile() and m.name.startswith(prefix)}  # fmt: skip
        url = f"{base}/{spec.vendor_id}/{spec.id}/{spec.psx_archive}/"
        checks.start()
        with contextlib.suppress(E2EError):
            wait_for("the web archive to be served as packaged", lambda: not archive_mismatches(url, packed), 180, 1)
        wrong = archive_mismatches(url, packed)
        checks.expect(not wrong, "web archive served", f"{len(packed)} files as packaged" if not wrong else
                      "; ".join(wrong[:6]) + " — urservice/nginx did not pick the archive up")  # fmt: skip
        if spec.daemon:
            checks.start()
            last = {"r": (0, b"")}

            def pong():
                last["r"] = http(base + backend_path(spec), timeout=10, data=xmlrpc_ping_body(), headers={"Content-Type": "text/xml"})
                return last["r"][0] == 200 and b"pong" in last["r"][1]

            try:
                wait_for("the backend container", pong, args.backend_timeout, 5, alive)
                checks.ok("backend answers ping", f"POST {backend_path(spec)} → pong")
            except E2EError:
                st, body = last["r"]
                checks.fail("backend answers ping",
                            f"POST {base}{backend_path(spec)} → HTTP {st} {body[:200]!r} after {args.backend_timeout:.0f} s — "
                            "see inner-docker.txt in the artifacts (the backend container's state and log)", fatal=False)  # fmt: skip
        if args.no_browser:
            checks.skip("browser", "--no-browser")
        else:
            browser_checks(checks, spec, port, out)
    except E2EError as exc:
        if not checks.items or checks.items[-1]["ok"] is not False:
            checks.fail("run", str(exc), fatal=False)
    except Exception as exc:  # noqa: BLE001 — playwright timeouts too
        checks.fail("run", f"{type(exc).__name__}: {str(exc)[:800]}", fatal=False)
    finally:
        if started:
            if out:
                with contextlib.suppress(Exception):
                    (out / "simulator.log").write_text(container_logs(name, 3000), encoding="utf-8")
                    ev = PSX_EVIDENCE % re.escape(spec.id)
                    (out / "inner-docker.txt").write_text(docker("exec", name, "sh", "-c", ev, timeout=120).stdout, encoding="utf-8")
            if args.keep:
                print(f"keeping {name}: http://127.0.0.1:{port} (docker rm -f -v {name})", flush=True)
            else:
                remove_container(name)
    report["passed"] = checks.passed
    report["seconds"] = round(time.monotonic() - t0, 1)
    _write_report(args, report)
    print(f"e2e-psx: {'passed' if checks.passed else 'FAILED'} — {spec.id} {spec.version} on PolyScope X {rel}", flush=True)
    return 0 if checks.passed else 1


# ==================================================================================================
# matrix / check-tags
# ==================================================================================================


def cmd_matrix(args) -> int:
    if args.floor:
        floor = args.floor
    else:
        repo = find(Path(args.repo) if args.repo else Path.cwd())
        floors = []
        for i in repo.urcap_ids():
            s = load(repo.urcap_dir(i))
            if args.platform in s.platforms:
                floors.append(s.ps5_floor if args.platform == "ps5" else s.psx_floor)
        if not floors:
            print("[]")
            return 0
        floor = min(floors, key=_key)
    vs = ps5_versions(floor) if args.platform == "ps5" else psx_versions(floor)
    print(json.dumps(vs, separators=(",", ":")))
    return 0


def cmd_check_tags(args) -> int:
    if args.platform == "ps5":
        hub = fetch_tags(PS5_IMAGE)
        problems = ps5_drift(hub)
        table = "PS5_MATRIX"
    else:
        hub = fetch_tags(PSX_IMAGE)
        problems = psx_drift(hub)
        table = "PSX_RELEASES"
    for p in problems:
        print(p)
    if problems:
        print(f"{len(problems)} problem(s): update {table} in urcapgen/e2e.py, run the e2e on the new version, "
              "then `urcapgen upgrade` the monorepos", file=sys.stderr)  # fmt: skip
        return 1
    print(f"{table} is current with Docker Hub")
    return 0


def _wrap(fn):
    def run(args) -> int:
        try:
            return fn(args)
        except E2EError as exc:
            print(f"urcapgen: {exc}", file=sys.stderr)
            return 1

    return run


def add_commands(sub) -> None:
    p = sub.add_parser("matrix", help="JSON list of simulator versions to test (CI matrix)")
    p.add_argument("platform", choices=("ps5", "psx"))
    p.add_argument("--floor", help="the oldest version (default: the lowest floor of the monorepo's URCaps)")
    p.set_defaults(fn=_wrap(cmd_matrix))

    p = sub.add_parser("check-tags", help="the simulator version tables vs Docker Hub; exit 1 on drift")
    p.add_argument("platform", choices=("ps5", "psx"))
    p.set_defaults(fn=_wrap(cmd_check_tags))

    for plat, fn in (("ps5", cmd_e2e_ps5), ("psx", cmd_e2e_psx)):
        what = "an e-Series URSim (PolyScope 5)" if plat == "ps5" else "a PolyScope X simulator"
        p = sub.add_parser(f"e2e-{plat}", help=f"boot {what}, install the built URCap, exercise it")
        p.add_argument("id")
        p.add_argument("--version", required=True, help="5.x or 5.x.y" if plat == "ps5" else "10.x or 10.x.y")
        p.add_argument("--keep", action="store_true", help="leave the simulator running")
        p.add_argument("--artifacts", help="directory for logs, screenshots and evidence")
        p.add_argument("--report", help="write the checks here as JSON")
        p.add_argument("--skip-below-floor", action="store_true",
                       help="exit 0 (reporting a skip) when --version is below the URCap's floor")
        p.add_argument("--boot-timeout", type=float, default=900, help="seconds to wait for the simulator (default 900)")
        if plat == "ps5":
            p.add_argument("--urcap-timeout", type=float, default=300, help="seconds for the bundle / daemon (default 300)")
            p.add_argument("--script-timeout", type=float, default=120, help="seconds for the URScript run (default 120)")
        else:
            p.add_argument("--port", type=int, default=0, help="host port for the web UI (default: a free one)")
            p.add_argument("--backend-timeout", type=float, default=300, help="seconds for the backend container (default 300)")
            p.add_argument("--no-browser", action="store_true", help="stop after the install checks")
        p.set_defaults(fn=_wrap(fn))

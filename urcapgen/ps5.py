"""Build, check and install a PolyScope 5 (e-Series) URCap (``.urcap``) — stdlib + a JDK.

A PolyScope 5 URCap is an OSGi bundle jar. UR's SDK builds it with Maven and the
maven-bundle-plugin; this does the same steps with ``javac`` / ``jdeps`` and :mod:`zipfile`:

* :func:`fetch_sdk` — the URCap API jars are not on Maven Central, and UR's SDK download sits
  behind a login, but every PolyScope 5 ships them as OSGi bundles in ``/ursim/GUI/bundle``.
  This copies the ones a URCap compiles against out of the e-Series URSim image of a version
  into ``<sdk_root>/<tag>/`` (never committed — they are UR's), with ``sdk.json``: what each
  jar exports and the URCap API version that PolyScope reports. By default it streams the
  image's layer straight from Docker Hub's registry — no Docker needed, any host.
* :func:`package` — compiles ``ps5/src`` for Java 8 (PolyScope 5 runs 1.8.0_371) against the
  **oldest supported PolyScope's** API jars (``compat.floor``), and each ``compat.since``
  group — code that uses newer API, reached only behind a runtime check — against that
  version's jars; derives ``Import-Package`` with ``jdeps``; writes the manifest with the two
  compatibility flags PolyScope's loader requires; embeds ``META-INF/maven/…/pom.xml`` naming
  the ``com.ur.urcap:api`` version (PolyScope 5.10+ reads it and refuses a newer one than it
  has); zips reproducibly (sorted entries, fixed timestamps, manifest first) with a digest of
  the sources in ``META-INF/urcapgen-sources.sha256``.
* :func:`check` — does the URCap work with a given PolyScope's API: the sources it loads
  compile against its jars, and every package the jar imports is one it exports.
* :func:`install` — into an e-Series URSim container (``/urcaps`` + restart).
* :func:`render_magic` — the USB stick's ``urmagic_<id>.sh`` for one built jar.

A JDK 11+ is found as ``$URCAPGEN_JDK/bin``, ``$JAVA_HOME/bin``, on ``PATH``, or — failing
all — run from the ``eclipse-temurin:21-jdk`` image with Docker (set ``URCAPGEN_JDK=docker``
to force it).
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from importlib import resources
from pathlib import Path

from .spec import Spec

API_RANGE = "[1.0.0,2.0.0)"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)  # zip entries' timestamp: the build is content-only
DIGEST_ENTRY = "META-INF/urcapgen-sources.sha256"
FALLBACK_API_VERSION = "1.7.0"  # PolyScope 5.4's; 5.5–5.9 report none (and read no pom)
REQUIRED_KEYS = (
    "Bundle-SymbolicName",
    "Bundle-Name",
    "Bundle-Vendor",
    "Bundle-Version",
    "Bundle-Activator",
    "URCapCompatibility-CB3",
    "URCapCompatibility-eSeries",
)
JDK_IMAGE = "eclipse-temurin:21-jdk"


class Urcap5Error(RuntimeError):
    pass


# -- properties + manifest ------------------------------------------------------------------


def read_properties(text: str) -> dict[str, str]:
    """``key=value`` lines; ``#`` comments and blank lines ignored."""
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise Urcap5Error(f"bundle.properties line {n}: expected key=value")
        out[key.strip()] = value.strip()
    missing = [k for k in REQUIRED_KEYS if not out.get(k)]
    if missing:
        raise Urcap5Error(f"bundle.properties is missing {', '.join(missing)}")
    for flag in ("URCapCompatibility-CB3", "URCapCompatibility-eSeries"):
        if out[flag] not in ("true", "false"):
            raise Urcap5Error(f"{flag} must be true or false")
    if out["URCapCompatibility-CB3"] == out["URCapCompatibility-eSeries"] == "false":
        raise Urcap5Error("a URCap compatible with neither CB3 nor e-Series will not load")
    return out


def manifest_line(key: str, value: str) -> bytes:
    """One JAR-manifest header, wrapped at 72 bytes with a leading-space continuation."""
    raw = f"{key}: {value}".encode()
    lines = [raw[:72]]
    rest = raw[72:]
    while rest:
        lines.append(b" " + rest[:71])
        rest = rest[71:]
    return b"\r\n".join(lines) + b"\r\n"


def import_package(packages: list[str], optional: list[str] | tuple[str, ...] = ()) -> str:
    """``Import-Package`` for the packages the classes reference (``java.*`` is never
    imported in OSGi; the URCap API gets a major-version range; ``optional`` packages —
    newer than the oldest supported PolyScope — must not stop the bundle resolving)."""
    parts = []
    for pkg in sorted(set(packages)):
        if pkg.startswith("java.") or pkg == "java":
            continue
        clause = f'{pkg};version="{API_RANGE}"' if pkg.startswith("com.ur.urcap.api") else pkg
        parts.append(clause + (";resolution:=optional" if pkg in optional else ""))
    return ",".join(parts)


def build_manifest(
    props: dict[str, str], packages: list[str], optional: list[str] | tuple[str, ...] = ()
) -> bytes:
    headers = [
        ("Manifest-Version", "1.0"),
        ("Bundle-ManifestVersion", "2"),
        ("Bundle-SymbolicName", props["Bundle-SymbolicName"]),
        ("Bundle-Name", props["Bundle-Name"]),
        ("Bundle-Vendor", props["Bundle-Vendor"]),
        ("Bundle-Version", props["Bundle-Version"]),
        ("Bundle-Activator", props["Bundle-Activator"]),
        # PolyScope 5's installer refuses a jar without it (URCapsServiceImpl.isValidFile).
        ("Bundle-Category", "URCap"),
        ("Bundle-RequiredExecutionEnvironment", "JavaSE-1.8"),
        ("Import-Package", import_package(packages, optional)),
        ("URCapCompatibility-CB3", props["URCapCompatibility-CB3"]),
        ("URCapCompatibility-eSeries", props["URCapCompatibility-eSeries"]),
    ]
    if props.get("Bundle-Description"):
        headers.insert(6, ("Bundle-Description", props["Bundle-Description"]))
    if props.get("Bundle-Copyright"):
        headers.insert(5, ("Bundle-Copyright", props["Bundle-Copyright"]))
    return b"".join(manifest_line(k, v) for k, v in headers) + b"\r\n"


def pom_xml(props: dict[str, str], api_version: str) -> tuple[str, bytes]:
    """The embedded pom PolyScope 5 reads the URCap API version from."""
    sym = props["Bundle-SymbolicName"]
    group, _, artifact = sym.rpartition(".")
    group = group or sym
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"  <groupId>{group}</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        f"  <version>{props['Bundle-Version']}</version>\n"
        "  <packaging>bundle</packaging>\n"
        f"  <name>{_xml(props['Bundle-Name'])}</name>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>com.ur.urcap</groupId>\n"
        "      <artifactId>api</artifactId>\n"
        f"      <version>{api_version}</version>\n"
        "      <scope>provided</scope>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )
    return f"META-INF/maven/{group}/{artifact}/pom.xml", body.encode()


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -- sources + compatibility plan -----------------------------------------------------------


def _files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def resource_dirs(spec: Spec) -> list[tuple[str, Path]]:
    """``(path prefix in the jar, directory)`` of every resource directory: ``ps5/resources/``
    at the jar's root, and the daemon's directory at ``<package>/daemon/``."""
    out = []
    if (spec.root / "ps5" / "resources").is_dir():
        out.append(("", spec.root / "ps5" / "resources"))
    if spec.daemon:
        from . import daemon

        out.append((daemon.jar_dir(spec) + "/", daemon.daemon_dir(spec)))
    return out


def sources_digest(spec: Spec) -> str:
    """SHA-256 over ``bundle.properties``, every ``src/**/*.java`` and every resource (path +
    bytes, sorted): the jar carries it, so a stale build is visible without a JDK."""
    src = spec.root / "ps5"
    h = hashlib.sha256()
    named = [("bundle.properties", src / "bundle.properties")]
    named += [(p.relative_to(src).as_posix(), p) for p in sorted((src / "src").rglob("*.java"))]
    for prefix, d in resource_dirs(spec):
        named += [("resource:" + prefix + p.relative_to(d).as_posix(), p) for p in _files(d)]
    for rel, f in named:
        h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def version_key(version: str) -> tuple[int, ...]:
    """``"5.8"`` / ``"5.10.2"`` → ``(5, 8)`` / ``(5, 10, 2)`` — numeric, never lexical."""
    if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){1,3}", version or ""):  # not \d: it takes any script
        raise Urcap5Error(f"{version!r} is not a version like 5.8 or 5.10.2")
    return tuple(int(p) for p in version.split("."))


def compat_plan(props: dict[str, str]) -> dict:
    """What ``bundle.properties`` says about the PolyScope versions the URCap runs on:

    * ``compat.floor`` — the oldest PolyScope 5 it supports. Everything is compiled against
      that release's URCap API jars, so a call the floor lacks fails the build, not a pendant.
    * ``compat.since.<version>`` — source files (under ``src/``) that use API newer than the
      floor. They are compiled against that version's jars, and the rest of the code only
      reaches them behind a runtime check (by class name, never a static reference).
    * ``compat.optional-packages`` — the packages only those files import: OSGi must not
      refuse the bundle on a PolyScope that lacks them (``resolution:=optional``).
    """
    floor = props.get("compat.floor", "")
    version_key(floor)
    since: dict[str, list[str]] = {}
    for key, value in props.items():
        if key.startswith("compat.since."):
            v = key[len("compat.since.") :]
            if version_key(v) <= version_key(floor):
                raise Urcap5Error(f"{key}: {v} is not newer than compat.floor={floor}")
            files = [f.strip() for f in value.split(",") if f.strip()]
            if not files:
                raise Urcap5Error(f"{key} names no source file")
            since[v] = files
    optional = sorted(p.strip() for p in props.get("compat.optional-packages", "").split(",") if p.strip())
    return {
        "floor": floor,
        "since": dict(sorted(since.items(), key=lambda kv: version_key(kv[0]))),
        "optional": optional,
    }


def sources_for(src: Path, plan: dict, version: str | None = None) -> list[Path]:
    """The sources a PolyScope ``version`` loads: every ``src/**/*.java`` except the
    ``compat.since`` groups newer than it (``None``: the floor — only the base code)."""
    base = src / "src"
    every = sorted(base.rglob("*.java"))
    known = {p.relative_to(base).as_posix() for p in every}
    target = version_key(version) if version else version_key(plan["floor"])
    skip: set[str] = set()
    for v, files in plan["since"].items():
        for f in files:
            if f not in known:
                raise Urcap5Error(f"compat.since.{v} names {f}, which is not under {base}")
        if version_key(v) > target:
            skip.update(files)
    return [p for p in every if p.relative_to(base).as_posix() not in skip]


# -- the URCap API jars out of a PolyScope 5 URSim image --------------------------------------
#
# Under names that change with the release (5.4: polyscope-urcap/api-1.7.0.jar; 5.5+:
# polyscope-urcap-urcap-api-*.jar plus a growing set of *-urcap-api* / urcap-api-export-*
# bundles, some in subdirectories up to 5.7), so the jars are picked by what they export, not by
# name: every bundle exporting a com.ur.urcap.api package, and the bundle exporting the newest
# org.osgi.framework (5.13+ ship both org.osgi.core-4.1.0 and osgi.core-6.0.0).

IMAGE_REPO = "universalrobots/ursim_e-series"
SDK_INFO = "sdk.json"
_REGISTRY = "https://registry-1.docker.io/v2/"
_TOKEN = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
_ACCEPT = ",".join(
    (
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    )
)
_BUNDLE = "ursim/GUI/bundle/"
_PROVIDER = re.compile(r"PackageDependencyProvider(\d+)_(\d+)_(\d+)Impl\.class$")


def split_image(image: str) -> tuple[str, str]:
    """``5.4`` / ``ursim_e-series:5.4`` / ``universalrobots/ursim_e-series:5.4`` → (repo, tag)."""
    repo, _, tag = image.rpartition(":")
    if not repo:
        repo = IMAGE_REPO
    elif "/" not in repo:
        repo = "universalrobots/" + repo
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise Urcap5Error(f"{image!r} has no image tag")
    return repo, tag


def split_clauses(header: str) -> list[str]:
    """An OSGi header's clauses: split on commas outside double quotes."""
    parts, cur, quoted = [], "", False
    for ch in header:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p for p in parts if p.strip()]


def parse_exports(header: str) -> dict[str, str | None]:
    """``Export-Package`` → ``{package: version or None}``."""
    out: dict[str, str | None] = {}
    for part in split_clauses(header):
        name = part.split(";", 1)[0].strip()
        m = re.search(r';\s*version\s*=\s*"?([0-9][0-9A-Za-z.\-_]*)', part)
        out[name] = m.group(1) if m else None
    return out


def _manifest(z: zipfile.ZipFile) -> dict[str, str]:
    raw = z.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    key = None
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith(" ") and key:
            headers[key] += line[1:]
        elif ": " in line:
            key, _, value = line.partition(": ")
            headers[key] = value
    return headers


def scan_bundles(files) -> dict:
    """From ``(path, bytes)`` of every jar in a PolyScope's bundle directory: the jars a URCap
    compiles against, what each exports, and the URCap API version this PolyScope reports
    (the newest ``PackageDependencyProvider<x_y_z>Impl`` in polyscope-urcap-impl, which
    PolyScope 5.10+ checks a URCap's embedded pom against; 5.4 ships the API as one Maven
    artifact with pom.properties; 5.5–5.9 record it in neither place: None)."""
    api: dict[str, bytes] = {}
    exports: dict[str, dict[str, str | None]] = {}
    osgi: list[tuple[tuple[int, ...], str, bytes]] = []
    providers: set[tuple[int, int, int]] = set()
    maven_api: str | None = None
    for path, data in files:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            names = z.namelist()
            headers = _manifest(z)
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
        for n in names:
            m = _PROVIDER.search(n)
            if m:
                providers.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
            if n.endswith("pom.properties"):
                pom = dict(
                    line.split("=", 1) for line in z.read(n).decode(errors="replace").splitlines() if "=" in line
                )
                if pom.get("groupId") == "com.ur.urcap" and pom.get("artifactId") == "api":
                    maven_api = pom.get("version")
        exp = parse_exports(headers.get("Export-Package", ""))
        name = path.rsplit("/", 1)[-1]
        if any(p.startswith("com.ur.urcap.api") for p in exp):
            api[name] = data
            exports[name] = {p: v for p, v in exp.items() if p.startswith("com.ur.urcap.api")}
        if "org.osgi.framework" in exp:
            v = exp["org.osgi.framework"] or "0"
            osgi.append((tuple(int(x) for x in re.findall(r"\d+", v)[:3]), name, data))
    if not api:
        raise Urcap5Error("no bundle exporting com.ur.urcap.api — not a PolyScope 5 bundle directory?")
    if not osgi:
        raise Urcap5Error("no bundle exporting org.osgi.framework")
    osgi_version, osgi_name, osgi_data = max(osgi)
    jars = {**api, osgi_name: osgi_data}
    exports[osgi_name] = {"org.osgi.framework": ".".join(map(str, osgi_version))}
    api_version = ".".join(map(str, max(providers))) if providers else maven_api
    return {"jars": jars, "exports": exports, "api_version": api_version}


def _http(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=120)  # noqa: S310 — fixed https registry URLs


def registry_image(image: str) -> tuple[str, str, dict, dict, dict]:
    """``(repo, tag, auth headers, manifest, config)`` of ``image``'s amd64 image."""
    repo, tag = split_image(image)
    with _http(_TOKEN.format(repo=repo)) as r:
        token = json.load(r)["token"]
    auth = {"Authorization": f"Bearer {token}"}
    with _http(f"{_REGISTRY}{repo}/manifests/{tag}", {**auth, "Accept": _ACCEPT}) as r:
        manifest = json.load(r)
    if "manifests" in manifest:  # an index: the amd64 image
        amd64 = [m for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == "amd64"]
        if not amd64:
            raise Urcap5Error(f"{repo}:{tag} has no amd64 image")
        with _http(f"{_REGISTRY}{repo}/manifests/{amd64[0]['digest']}", {**auth, "Accept": _ACCEPT}) as r:
            manifest = json.load(r)
    with _http(f"{_REGISTRY}{repo}/blobs/{manifest['config']['digest']}", auth) as r:
        config = json.load(r)
    return repo, tag, auth, manifest, config


def registry_bundle_files(image: str):
    """Every jar under ``/ursim/GUI/bundle`` of ``image``, straight from Docker Hub's registry:
    the layer the image's history says ``COPY ursim_<version> /ursim`` made, streamed."""
    repo, tag, auth, manifest, config = registry_image(image)
    history = [h for h in config.get("history", []) if not h.get("empty_layer")]
    layers = manifest["layers"]
    picks = [i for i, h in enumerate(history) if "COPY ursim_" in h.get("created_by", "")]
    if len(history) != len(layers) or len(picks) != 1:
        raise Urcap5Error(f"{repo}:{tag}: no single `COPY ursim_*` layer in its history")
    with _http(f"{_REGISTRY}{repo}/blobs/{layers[picks[0]]['digest']}", auth) as r:
        with tarfile.open(fileobj=r, mode="r|gz") as tar:
            for member in tar:
                if member.isfile() and member.name.startswith(_BUNDLE) and member.name.endswith(".jar"):
                    f = tar.extractfile(member)
                    if f is not None:
                        yield member.name[len(_BUNDLE) :], f.read()


def docker_bundle_files(image: str):
    """The same jars via a local Docker: ``docker create`` + ``docker cp`` (nothing runs)."""
    docker = os.environ.get("DOCKER", "docker").split()
    if not shutil.which(docker[0]):
        raise Urcap5Error("docker is not on PATH (or use --source registry)")
    repo, tag = split_image(image)
    cid = subprocess.run(
        [*docker, "create", "--platform", "linux/amd64", f"{repo}:{tag}"], capture_output=True, text=True, check=False
    )
    if cid.returncode != 0:
        raise Urcap5Error(f"docker create {repo}:{tag} failed: {cid.stderr.strip()}")
    container = cid.stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cp = subprocess.run(
                [*docker, "cp", f"{container}:/{_BUNDLE}", tmp], capture_output=True, text=True, check=False
            )
            if cp.returncode != 0:
                raise Urcap5Error(f"docker cp failed: {cp.stderr.strip()}")
            root = Path(tmp) / "bundle"
            for jar in sorted(root.rglob("*.jar")):
                yield jar.relative_to(root).as_posix(), jar.read_bytes()
    finally:
        subprocess.run([*docker, "rm", container], capture_output=True, check=False)


def fetch_sdk(image: str, sdk_root: Path, *, source: str = "registry") -> dict:
    """Write ``image``'s URCap API jars and ``sdk.json`` into ``sdk_root/<tag>/``."""
    repo, tag = split_image(image)
    dest = sdk_root / tag
    files = registry_bundle_files(image) if source == "registry" else docker_bundle_files(image)
    found = scan_bundles(files)
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.jar"):
        old.unlink()
    for name, data in sorted(found["jars"].items()):
        (dest / name).write_bytes(data)
    info = {"image": f"{repo}:{tag}", "api_version": found["api_version"], "jars": sorted(found["jars"]), "exports": found["exports"]}
    (dest / SDK_INFO).write_text(json.dumps(info, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return {**info, "dir": str(dest)}


def sdk_info(sdk_dir: Path) -> dict:
    info = sdk_dir / SDK_INFO
    if not info.is_file() or not any(sdk_dir.glob("*.jar")):
        raise Urcap5Error(f"no URCap API jars in {sdk_dir} — run `urcapgen sdk --image {sdk_dir.name}` first")
    return json.loads(info.read_text(encoding="utf-8"))


def sdk_jars(sdk_dir: Path) -> list[Path]:
    sdk_info(sdk_dir)
    return sorted(sdk_dir.glob("*.jar"))


def ensure_sdk(version: str, sdk_root: Path) -> Path:
    """``sdk_root/<version>``, fetched from the registry when it isn't there yet."""
    d = sdk_root / version
    try:
        sdk_info(d)
    except Urcap5Error:
        fetch_sdk(version, sdk_root)
    return d


# -- the JDK ------------------------------------------------------------------------------------


def _jdk_bin() -> str | None:
    """The directory holding javac/jdeps/javap, ``"docker"`` for the container fallback, or None."""
    forced = os.environ.get("URCAPGEN_JDK", "")
    if forced == "docker":
        return "docker"
    for home in (forced, os.environ.get("JAVA_HOME", "")):
        if home and (Path(home) / "bin" / "javac").exists():
            return str(Path(home) / "bin")
    javac = shutil.which("javac")
    if javac:
        return str(Path(javac).resolve().parent)
    if shutil.which("docker"):
        return "docker"
    return None


def java_tool(name: str, args: list[str], mounts: list[Path]) -> subprocess.CompletedProcess:
    """Run a JDK tool. With the Docker fallback every directory in ``mounts`` is bind-mounted
    at the same path so the arguments mean the same inside the container."""
    where = _jdk_bin()
    if where is None:
        raise Urcap5Error(f"{name}: no JDK (set URCAPGEN_JDK or JAVA_HOME, put javac on PATH, or install Docker)")
    if where != "docker":
        return subprocess.run([str(Path(where) / name), *args], capture_output=True, text=True, check=False)
    vols: list[str] = []
    seen: set[str] = set()
    for m in mounts:
        p = str(Path(m).resolve())
        if p not in seen:
            seen.add(p)
            vols += ["-v", f"{p}:{p}"]
    uid = [] if os.name == "nt" else ["-u", f"{os.getuid()}:{os.getgid()}"]
    return subprocess.run(
        ["docker", "run", "--rm", *uid, *vols, JDK_IMAGE, name, *args], capture_output=True, text=True, check=False
    )


def referenced_packages(classes: Path, classpath: list[Path]) -> list[str]:
    """Packages the compiled classes use, from ``jdeps -verbose:package``."""
    out = java_tool(
        "jdeps",
        ["-verbose:package", "-cp", os.pathsep.join(str(p) for p in classpath), str(classes)],
        [classes, *{p.parent for p in classpath}],
    )
    if out.returncode != 0:
        raise Urcap5Error(f"jdeps failed: {out.stderr.strip() or out.stdout.strip()}")
    pkgs = set()
    for line in out.stdout.splitlines():
        if "->" not in line:
            continue
        target = line.split("->", 1)[1].split()
        if target and "." in target[0] and "/" not in target[0] and not target[0].endswith(".jar"):
            pkgs.add(target[0])
    return sorted(pkgs)


def javac(sources: list[Path], classpath: list[Path], out: Path) -> None:
    """``javac --release 8`` with every lint as an error."""
    cc = java_tool(
        "javac",
        [
            "--release", "8", "-Xlint:all", "-Xlint:-options", "-Werror", "-encoding", "UTF-8",
            "-cp", os.pathsep.join(str(j) for j in classpath), "-d", str(out), *[str(f) for f in sources],
        ],
        [out, *{s.parent for s in sources}, *{p if p.is_dir() else p.parent for p in classpath}],
    )  # fmt: skip
    if cc.returncode != 0:
        raise Urcap5Error("javac failed:\n" + (cc.stderr or cc.stdout))


# -- package --------------------------------------------------------------------------------


def api_version(floor_info: dict) -> str:
    """The ``com.ur.urcap:api`` version the embedded pom names: what the floor PolyScope
    reports. Newer and PolyScope 5.10+ refuse the URCap; a floor that reports none (5.5–5.9)
    gets 1.7.0 — older than true, which no PolyScope refuses."""
    return floor_info.get("api_version") or FALLBACK_API_VERSION


def _licence(spec: Spec) -> Path | None:
    for p in (spec.root / "LICENSE", spec.root.parent.parent / "LICENSE"):
        if p.is_file():
            return p
    return None


def package(spec: Spec, out_dir: str | Path, sdk_root: Path) -> Path:
    """Build ``<urcap>/ps5`` into ``out_dir/<id>-ps5-<version>.urcap``."""
    src, out_dir = spec.root / "ps5", Path(out_dir)
    props = read_properties((src / "bundle.properties").read_text(encoding="utf-8"))
    plan = compat_plan(props)
    base = sources_for(src, plan)
    if not base:
        raise Urcap5Error(f"no Java sources under {src / 'src'} — run `urcapgen gen` first")
    floor_dir = ensure_sdk(plan["floor"], sdk_root)
    api = api_version(sdk_info(floor_dir))
    used = sdk_jars(floor_dir)
    own = props["Bundle-SymbolicName"]
    with tempfile.TemporaryDirectory() as tmp:
        classes = Path(tmp) / "classes"
        classes.mkdir()
        javac(base, used, classes)
        for version, files in plan["since"].items():
            jars = sdk_jars(ensure_sdk(version, sdk_root))
            javac([src / "src" / f for f in files], [classes, *jars], classes)
            used += [j for j in jars if j not in used]
        packages = [p for p in referenced_packages(classes, used) if not (p == own or p.startswith(own + "."))]
        stale = [p for p in plan["optional"] if p not in packages]
        if stale:
            raise Urcap5Error(f"compat.optional-packages names {', '.join(stale)}, which no class imports")
        entries: list[tuple[str, bytes]] = [
            (p.relative_to(classes).as_posix(), p.read_bytes()) for p in sorted(classes.rglob("*.class"))
        ]
    for prefix, d in resource_dirs(spec):
        entries += [(prefix + p.relative_to(d).as_posix(), p.read_bytes()) for p in _files(d)]
    pom_path, pom = pom_xml(props, api)
    entries += [(pom_path, pom), (DIGEST_ENTRY, (sources_digest(spec) + "\n").encode())]
    licence = _licence(spec)
    if licence:
        entries.append(("META-INF/LICENSE", licence.read_bytes()))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / spec.ps5_file
    tmp_out = out.with_suffix(".urcap.tmp")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("META-INF/", FIXED_TIME), b"")
        _write(z, "META-INF/MANIFEST.MF", build_manifest(props, packages, plan["optional"]))
        for path, data in sorted(entries):
            _write(z, path, data, executable=path.endswith((".py", ".sh")))
    tmp_out.replace(out)
    return out


def _write(z: zipfile.ZipFile, path: str, data: bytes, *, executable: bool = False) -> None:
    info = zipfile.ZipInfo(path, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # unix, so the mode below is read
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    z.writestr(info, data)


def read_bundle(path: str | Path) -> dict:
    """The manifest headers, entry names, sources digest and embedded pom of a ``.urcap``."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        headers = _manifest(z)
        digest = z.read(DIGEST_ENTRY).decode().strip() if DIGEST_ENTRY in names else None
        poms = [n for n in names if n.startswith("META-INF/maven/") and n.endswith("/pom.xml")]
        pom = z.read(poms[0]).decode() if poms else ""
    return {"headers": headers, "names": names, "sources_sha256": digest, "pom": pom}


def install_checks(path: Path) -> list[str]:
    """What PolyScope 5's installer would refuse (URCapsServiceImpl / the bundle validator)."""
    b = read_bundle(path)
    h = b["headers"]
    problems = []
    if b["names"][:2] != ["META-INF/", "META-INF/MANIFEST.MF"]:
        problems.append("META-INF/MANIFEST.MF is not the first entry")
    if h.get("Bundle-Category", "").lower() != "urcap":
        problems.append("Bundle-Category is not URCap")
    for k in REQUIRED_KEYS:
        if not h.get(k):
            problems.append(f"no {k}")
    if not b["pom"] or "<artifactId>api</artifactId>" not in b["pom"]:
        problems.append("no embedded pom naming com.ur.urcap:api")
    return problems


# -- check against one PolyScope -------------------------------------------------------------


def imports_of(bundle: dict) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for clause in split_clauses(bundle["headers"].get("Import-Package", "")):
        name, *attrs = [a.strip() for a in clause.split(";")]
        pairs = (a.partition("=") for a in attrs)
        out[name] = {k.strip().rstrip(":"): v.strip().strip('"') for k, _, v in pairs}
    return out


def missing_imports(imports: dict[str, dict[str, str]], exported: set[str]) -> list[str]:
    return sorted(
        p
        for p, attrs in imports.items()
        if p.startswith(("com.ur.urcap.api", "org.osgi.")) and attrs.get("resolution") != "optional" and p not in exported
    )


def check(spec: Spec, sdk_dir: Path, dist: Path, version: str | None = None) -> dict:
    """Does the URCap work with the PolyScope whose API jars are in ``sdk_dir``?"""
    src = spec.root / "ps5"
    info = sdk_info(sdk_dir)
    version = version or split_image(info["image"])[1]
    props = read_properties((src / "bundle.properties").read_text(encoding="utf-8"))
    plan = compat_plan(props)
    if version_key(version) < version_key(plan["floor"]):
        raise Urcap5Error(f"PolyScope {version} is older than compat.floor={plan['floor']}")
    sources = sources_for(src, plan, version)
    with tempfile.TemporaryDirectory() as tmp:
        javac(sources, sdk_jars(sdk_dir), Path(tmp))
    exported = {p for pkgs in info["exports"].values() for p in pkgs}
    missing = missing_imports(imports_of(read_bundle(dist)), exported)
    if missing:
        raise Urcap5Error(f"{dist.name} imports {', '.join(missing)}, which PolyScope {version} does not export")
    return {"version": version, "image": info["image"], "api_version": info.get("api_version"), "compiled": len(sources)}


# -- install --------------------------------------------------------------------------------


def install(path: str | Path, container: str) -> dict:
    """Copy the jar into the URSim container's ``/urcaps`` and restart it (its entrypoint
    copies ``/urcaps/*.jar`` into PolyScope's bundle directory at start)."""
    path = Path(path)
    if not path.is_file():
        raise Urcap5Error(f"{path} does not exist")
    for cmd in (
        ["docker", "cp", str(path), f"{container}:/urcaps/{path.stem}.jar"],
        ["docker", "restart", container],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise Urcap5Error(f"{' '.join(cmd[:2])} failed: {r.stderr.strip()}")
    return {"ok": True, "container": container, "jar": f"/urcaps/{path.stem}.jar", "restarted": True}


# -- is a rebuilt jar the same as another? ------------------------------------------------------

_ANONYMOUS = re.compile(r"\$[0-9]+$")


def _member_name(line: str) -> str:
    head = line.split("(", 1)[0] if "(" in line else line.split(" =", 1)[0].rstrip(";")
    return head.split()[-1] if head.split() else ""


def _source_members(cls: str, lines: list[str]) -> list[str]:
    """What the sources declare, without what javac synthesises and names differently from one
    JDK major to the next (``$`` members, anonymous classes' constructors)."""
    out: list[str] = []
    skip = False
    for line in lines:
        if line.strip().startswith("descriptor:"):
            if not skip:
                out.append(line)
            continue
        skip = False
        if line.startswith("  ") and line.strip():
            name = _member_name(line.strip())
            simple = name.rsplit(".", 1)[-1]
            ctor = name == cls
            if ("$" in simple and not ctor) or (ctor and _ANONYMOUS.search(cls)):
                skip = True
                continue
        out.append(line)
    return out


def class_signatures(jar: Path) -> dict[str, str]:
    with zipfile.ZipFile(jar) as z:
        names = sorted(n[: -len(".class")].replace("/", ".") for n in z.namelist() if n.endswith(".class"))
    if not names:
        return {}
    r = java_tool("javap", ["-p", "-s", "-constants", "-cp", str(jar), *names], [jar.parent])
    if r.returncode != 0:
        raise Urcap5Error(f"javap failed: {r.stderr.strip()}")
    sigs: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if current is not None:
            sigs[current] = "\n".join(_source_members(current, lines))

    for line in r.stdout.splitlines():
        if line.startswith("Compiled from"):
            flush()
            current, lines = None, []
            continue
        if current is None:
            m = re.search(r"(?:class|interface|enum)\s+([\w.$]+)", line)
            if m:
                current = m.group(1)
        lines.append(line.rstrip())
    flush()
    return sigs


def compare_jars(built: Path, other: Path) -> list[str]:
    """Why ``built`` is not ``other``, one line each; ``[]`` when they match (same entries,
    non-class entries byte-identical, every class declaring the same members)."""
    problems: list[str] = []
    with zipfile.ZipFile(built) as a, zipfile.ZipFile(other) as b:
        na, nb = set(a.namelist()), set(b.namelist())
        problems += [f"{n}: only in {built.name}" for n in sorted(na - nb)]
        problems += [f"{n}: only in {other.name}" for n in sorted(nb - na)]
        problems += [f"{n}: differs" for n in sorted(na & nb) if not n.endswith(".class") and a.read(n) != b.read(n)]
    if problems:
        return problems
    sa, sb = class_signatures(built), class_signatures(other)
    for cls in sorted(set(sa) | set(sb)):
        if sa.get(cls) != sb.get(cls):
            diff = difflib.unified_diff((sb.get(cls) or "").splitlines(), (sa.get(cls) or "").splitlines(), n=0, lineterm="")
            shown = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))][:6]
            problems.append(f"{cls}: its members differ " + " | ".join(shown))
    return problems


# -- the USB stick's magic file -----------------------------------------------------------------


def render_magic(spec: Spec, urcap: Path) -> str:
    template = (resources.files("urcapgen") / "runtime" / "urmagic.sh.in").read_text(encoding="utf-8")
    safe_name = re.sub(r'["$`\\]', "", spec.name)
    values = {
        "@MAGIC_FILE@": spec.magic_file,
        "@LOG_FILE@": spec.magic_file.replace(".sh", ".log"),
        "@URCAP_NAME@": safe_name,
        "@URCAP_FILE@": urcap.name,
        "@URCAP_SHA256@": hashlib.sha256(urcap.read_bytes()).hexdigest(),
        "@SYMBOLIC_NAME@": spec.symbolic_name,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def write_magic(spec: Spec, urcap: Path) -> Path:
    path = urcap.parent / spec.magic_file
    path.write_bytes(render_magic(spec, urcap).encode("utf-8"))
    path.chmod(0o755)
    return path

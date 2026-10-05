"""``urcap.toml``: the one source both URCaps are generated from.

:func:`load` reads and validates a spec and returns a :class:`Spec` with every derived name
the generators need (Java package and class names, PolyScope X tags, file names), so no
generator invents a name of its own. The spec format is documented in
``skill/references/spec.md``; this module is what enforces it.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import scripttpl

SPEC_FILE = "urcap.toml"
ID = re.compile(r"[a-z][a-z0-9-]*[a-z0-9]")  # manifest-spec urcapID / vendorID, no underscore
KEY = re.compile(r"[a-z][a-z0-9_]*")
NODE_ID = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
SEMVER = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
JAVA_PACKAGE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
FIELD_TYPES = ("int", "float", "bool", "string", "choice", "pose")
PS5_FLOOR_MIN = "5.4"
PSX_FLOOR_MIN = "10.8"
SIDEBAR_FLOOR = "10.10"
JAVA_RESERVED = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const",
    "continue", "default", "do", "double", "else", "enum", "extends", "final", "finally", "float",
    "for", "goto", "if", "implements", "import", "instanceof", "int", "interface", "long", "native",
    "new", "package", "private", "protected", "public", "return", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try", "void",
    "volatile", "while", "true", "false", "null",
}  # fmt: skip


class SpecError(ValueError):
    pass


def version_key(v: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){1,3}", v or ""):
        raise SpecError(f"{v!r} is not a version like 5.4 or 10.13")
    return tuple(int(p) for p in v.split("."))


def camel(s: str) -> str:
    """``gripper_io`` / ``gripper-io`` → ``GripperIo``."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_]", s) if p)


@dataclass
class Field:
    key: str
    type: str
    label: str
    default: object
    unit: str = ""
    help: str = ""
    min: float | None = None
    max: float | None = None
    options: list[dict] = field(default_factory=list)  # choice: [{value, label}]

    def as_json(self) -> dict:
        out = {"key": self.key, "type": self.type, "label": self.label, "default": self.default}
        for k in ("unit", "help"):
            if getattr(self, k):
                out[k] = getattr(self, k)
        for k in ("min", "max"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        if self.options:
            out["options"] = self.options
        return out


@dataclass
class Node:
    kind: str  # "installation" | "program" | "toolbar"
    id: str
    title: str
    description: str
    fields: list[Field]
    script: str = ""
    script_after: str = ""
    label: str = ""
    allows_children: bool = False

    @property
    def cls(self) -> str:
        """The Java class prefix: ``GripService``, ``GripContribution``, ``GripView``."""
        return camel(self.id)


@dataclass
class Daemon:
    port: int
    entry: str  # relative to the URCap's directory, user-owned


@dataclass
class Spec:
    root: Path
    id: str
    name: str
    version: str
    description: str
    vendor_id: str
    vendor_name: str
    java_package: str
    ps5_floor: str
    psx_floor: str
    platforms: tuple[str, ...]
    installation: Node | None
    programs: list[Node]
    toolbar: Node | None
    daemon: Daemon | None
    raw: dict
    notes: list[str] = field(default_factory=list)

    # -- derived names: the only place they are made -------------------------------------
    @property
    def symbolic_name(self) -> str:
        return self.java_package

    @property
    def java_dir(self) -> str:
        return self.java_package.replace(".", "/")

    @property
    def ps5_file(self) -> str:
        return f"{self.id}-ps5-{self.version}.urcap"

    @property
    def psx_file(self) -> str:
        return f"{self.id}-{self.version}.urcapx"

    @property
    def magic_file(self) -> str:
        return f"urmagic_{self.id.replace('-', '_')}.sh"

    @property
    def tag_prefix(self) -> str:
        return f"{self.id}-v"

    @property
    def psx_archive(self) -> str:
        """The PolyScope X web archive folder (and id)."""
        return f"{self.id}-frontend"

    @property
    def psx_backend(self) -> str:
        return f"{self.id}-backend"

    def psx_tag(self, node: Node | None = None) -> str:
        base = f"{self.vendor_id}-{self.id}"
        if node is None or node.kind == "installation":
            return base
        return f"{base}-{node.id.replace('_', '-')}"

    @property
    def nodes(self) -> list[Node]:
        return [n for n in (self.installation, *self.programs, self.toolbar) if n is not None]


# -- loading ---------------------------------------------------------------------------------


def _req(table: dict, key: str, where: str, kind=str):
    if key not in table:
        raise SpecError(f"{where}: `{key}` is required")
    v = table[key]
    if not isinstance(v, kind):
        raise SpecError(f"{where}.{key}: expected {kind.__name__ if isinstance(kind, type) else kind}")
    return v


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SpecError(f"{where}: expected a number")
    return float(v)


def _field(t: dict, where: str) -> Field:
    key = _req(t, "key", where)
    where = f"{where}[{key}]"
    if not KEY.fullmatch(key) or key in JAVA_RESERVED:
        raise SpecError(f"{where}: key must match ^[a-z][a-z0-9_]*$ and not be a Java keyword")
    kind = _req(t, "type", where)
    if kind not in FIELD_TYPES:
        raise SpecError(f"{where}: type must be one of {', '.join(FIELD_TYPES)}")
    f = Field(
        key=key,
        type=kind,
        label=t.get("label") or key.replace("_", " ").capitalize(),
        default=None,
        unit=t.get("unit", ""),
        help=t.get("help", ""),
    )
    unknown = set(t) - {"key", "type", "label", "default", "unit", "help", "min", "max", "options"}
    if unknown:
        raise SpecError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
    if kind in ("int", "float"):
        f.min = _num(t["min"], f"{where}.min") if "min" in t else None
        f.max = _num(t["max"], f"{where}.max") if "max" in t else None
        if f.min is not None and f.max is not None and f.min > f.max:
            raise SpecError(f"{where}: min > max")
        d = t.get("default", f.min if f.min is not None and f.min > 0 else 0)
        d = _num(d, f"{where}.default")
        if kind == "int":
            if d != int(d):
                raise SpecError(f"{where}.default: an int field needs a whole number")
            d = int(d)
        if (f.min is not None and d < f.min) or (f.max is not None and d > f.max):
            raise SpecError(f"{where}.default: outside min/max")
        f.default = d
    elif kind == "bool":
        d = t.get("default", False)
        if not isinstance(d, bool):
            raise SpecError(f"{where}.default: expected true or false")
        f.default = d
    elif kind == "string":
        d = t.get("default", "")
        if not isinstance(d, str) or '"' in d or "\n" in d:
            raise SpecError(f"{where}.default: a string without double quotes or line breaks")
        f.default = d
    elif kind == "choice":
        opts = t.get("options")
        if not isinstance(opts, list) or not opts:
            raise SpecError(f"{where}: a choice needs options = [{{value = ..., label = ...}}, ...]")
        norm = []
        for i, o in enumerate(opts):
            if isinstance(o, str):
                o = {"value": o}
            if not isinstance(o, dict) or "value" not in o:
                raise SpecError(f"{where}.options[{i}]: needs a value")
            v = str(o["value"])
            if not v or "\n" in v or "}}" in v:
                raise SpecError(f"{where}.options[{i}]: value must be a one-line URScript literal")
            norm.append({"value": v, "label": str(o.get("label", v))})
        if len({o["value"] for o in norm}) != len(norm):
            raise SpecError(f"{where}: option values repeat")
        f.options = norm
        d = str(t.get("default", norm[0]["value"]))
        if d not in {o["value"] for o in norm}:
            raise SpecError(f"{where}.default: not one of the option values")
        f.default = d
    elif kind == "pose":
        d = t.get("default", [0.0] * 6)
        if not isinstance(d, list) or len(d) != 6:
            raise SpecError(f"{where}.default: six numbers [x, y, z, rx, ry, rz] (m, rad)")
        f.default = [_num(v, f"{where}.default") for v in d]
    if kind in ("bool", "string", "choice", "pose") and ("min" in t or "max" in t):
        raise SpecError(f"{where}: min/max only apply to int and float")
    return f


def _fields(t: dict, where: str) -> list[Field]:
    raw = t.get("fields", [])
    if not isinstance(raw, list):
        raise SpecError(f"{where}.fields: expected [[{where}.fields]] tables")
    out = [_field(f, f"{where}.fields") for f in raw]
    keys = [f.key for f in out]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        raise SpecError(f"{where}: field keys repeat: {', '.join(sorted(dup))}")
    return out


def _check_template(text: str, fields: list[Field], where: str) -> None:
    try:
        used = scripttpl.names(text)
    except scripttpl.TemplateError as exc:
        raise SpecError(f"{where}: {exc}") from None
    missing = used - {f.key for f in fields}
    if missing:
        raise SpecError(f"{where}: uses {', '.join(sorted(missing))}, which are not fields of this node")
    try:  # every default renders
        scripttpl.render(text, {f.key: (f.type, f.default) for f in fields})
    except scripttpl.TemplateError as exc:
        raise SpecError(f"{where}: {exc}") from None


def load(path: str | Path) -> Spec:
    path = Path(path)
    if path.is_dir():
        path = path / SPEC_FILE
    if not path.is_file():
        raise SpecError(f"no {SPEC_FILE} at {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{path}: {exc}") from None
    return parse(data, path.parent)


def parse(data: dict, root: Path) -> Spec:
    known = {"urcap", "vendor", "compat", "installation", "program", "toolbar", "daemon"}
    unknown = set(data) - known
    if unknown:
        raise SpecError(f"unknown tables: {', '.join(sorted(unknown))}")
    u = _req(data, "urcap", "spec", dict)
    v = _req(data, "vendor", "spec", dict)
    uid = _req(u, "id", "urcap")
    if not ID.fullmatch(uid):
        raise SpecError("urcap.id must match ^[a-z][a-z0-9-]*[a-z0-9]$ (it becomes the urcapID)")
    vid = _req(v, "id", "vendor")
    if not ID.fullmatch(vid):
        raise SpecError("vendor.id must match ^[a-z][a-z0-9-]*[a-z0-9]$ (it becomes the vendorID)")
    version = _req(u, "version", "urcap")
    if not SEMVER.fullmatch(version):
        raise SpecError("urcap.version must be major.minor.patch")
    pkg = v.get("java_package") or f"com.{vid.replace('-', '')}.{uid.replace('-', '')}"
    if not JAVA_PACKAGE.fullmatch(pkg) or any(p in JAVA_RESERVED for p in pkg.split(".")):
        raise SpecError(f"vendor.java_package {pkg!r} is not a valid Java package")
    c = data.get("compat", {})
    ps5_floor = str(c.get("ps5_floor", PS5_FLOOR_MIN))
    psx_floor = str(c.get("psx_floor", PSX_FLOOR_MIN))
    if version_key(ps5_floor) < version_key(PS5_FLOOR_MIN):
        raise SpecError(f"compat.ps5_floor: urcapgen supports PolyScope {PS5_FLOOR_MIN} and later")
    if version_key(psx_floor) < version_key(PSX_FLOOR_MIN):
        raise SpecError(f"compat.psx_floor: urcapgen supports PolyScope X {PSX_FLOOR_MIN} and later")
    platforms = tuple(c.get("platforms", ["ps5", "psx"]))
    if not platforms or set(platforms) - {"ps5", "psx"}:
        raise SpecError('compat.platforms: a non-empty subset of ["ps5", "psx"]')

    inst = None
    if "installation" in data:
        t = data["installation"]
        if not isinstance(t, dict):
            raise SpecError("[installation] is one table (a URCap has at most one installation node)")
        fields = _fields(t, "installation")
        inst = Node(
            kind="installation",
            id=uid.replace("-", "_"),
            title=t.get("title", u.get("name", uid)),
            description=t.get("description", ""),
            fields=fields,
            script=t.get("script", ""),
        )
        _check_template(inst.script, fields, "installation.script")

    programs: list[Node] = []
    for i, t in enumerate(data.get("program", [])):
        nid = _req(t, "id", f"program[{i}]")
        if not NODE_ID.fullmatch(nid):
            raise SpecError(f"program[{i}].id must match ^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
        where = f"program[{nid}]"
        fields = _fields(t, where)
        n = Node(
            kind="program",
            id=nid,
            title=_req(t, "title", where),
            description=t.get("description", ""),
            fields=fields,
            script=t.get("script", ""),
            script_after=t.get("script_after", ""),
            label=t.get("label", ""),
            allows_children=bool(t.get("allows_children", False)),
        )
        if n.script_after and not n.allows_children:
            raise SpecError(f"{where}: script_after needs allows_children = true")
        _check_template(n.script, fields, f"{where}.script")
        _check_template(n.script_after, fields, f"{where}.script_after")
        _check_template(n.label, fields, f"{where}.label")
        programs.append(n)
    ids = [p.id for p in programs]
    if len(set(ids)) != len(ids):
        raise SpecError("program ids repeat")
    if inst and inst.cls in {p.cls for p in programs}:
        raise SpecError(f"a program node may not be named like the installation node ({inst.id})")

    toolbar = None
    if "toolbar" in data:
        t = data["toolbar"]
        toolbar = Node(
            kind="toolbar",
            id="toolbar",
            title=t.get("title", u.get("name", uid)),
            description=t.get("description", ""),
            fields=[],
        )

    daemon = None
    if "daemon" in data:
        t = data["daemon"]
        port = t.get("port", 40405)
        if not isinstance(port, int) or not 1024 < port < 65536:
            raise SpecError("daemon.port: an int between 1025 and 65535")
        entry = t.get("entry", "daemon/daemon.py")
        if entry.startswith("/") or ".." in Path(entry).parts:
            raise SpecError("daemon.entry: a path inside the URCap's directory")
        daemon = Daemon(port=port, entry=entry)
        if inst is None:
            raise SpecError("[daemon] needs an [installation] node (it starts and stops the daemon)")

    notes: list[str] = []
    if toolbar and version_key(psx_floor) < version_key(SIDEBAR_FLOOR) and "psx" in platforms:
        # UR's first sidebar sample is SDK 6.2.44 (PolyScope X 10.10); older releases rendering
        # one is unverified, so a URCap with a toolbar does not claim them.
        notes.append(f"compat.psx_floor raised from {psx_floor} to {SIDEBAR_FLOOR}: [toolbar] is a sidebar item on PolyScope X")
        psx_floor = SIDEBAR_FLOOR

    if not (inst or programs or toolbar):
        raise SpecError("the URCap contributes nothing: add [installation], [[program]] or [toolbar]")

    return Spec(
        root=root,
        id=uid,
        name=u.get("name", uid),
        version=version,
        description=u.get("description", ""),
        vendor_id=vid,
        vendor_name=_req(v, "name", "vendor"),
        java_package=pkg,
        ps5_floor=ps5_floor,
        psx_floor=psx_floor,
        platforms=platforms,
        installation=inst,
        programs=programs,
        toolbar=toolbar,
        daemon=daemon,
        raw=data,
        notes=notes,
    )

"""The URCap monorepo: where URCaps live, and how it gets (and refreshes) its tooling.

Layout (``urcapgen init`` makes it; ``urcapgen upgrade`` refreshes the managed parts)::

    .urcapgen                   marker + the vendored urcapgen version (JSON)
    urcapgen                    wrapper: ./urcapgen <command> (runs tools/urcapgen)
    tools/urcapgen/             this package, vendored            (managed)
    .claude/skills/urcap/       the skill: SKILL.md + references/ (managed)
    .github/workflows/urcapgen-*.yml   CI and CD                  (managed)
    AGENTS.md, CLAUDE.md, README.md, .gitignore                   (created once; yours)
    urcaps/<id>/urcap.toml      one directory per URCap; ps5/ and psx/ generated from it
    dist/                       build output (git-ignored)
    .urcapgen-cache/            PolyScope 5 API jars etc. (git-ignored)
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import __version__
from .spec import ID, SEMVER, SpecError, load

MARKER = ".urcapgen"
TEMPLATE = resources.files("urcapgen") / "template"
MANAGED_DIRS = ("tools/urcapgen", ".claude/skills/urcap")
MANAGED_GLOBS = (".github/workflows/urcapgen-*.yml",)
CREATED_ONCE = ("AGENTS.md", "CLAUDE.md", "README.md", ".gitignore", "urcaps/.gitkeep")


class RepoError(RuntimeError):
    pass


@dataclass
class Repo:
    root: Path

    @property
    def urcaps(self) -> Path:
        return self.root / "urcaps"

    @property
    def dist(self) -> Path:
        return self.root / "dist"

    @property
    def cache(self) -> Path:
        return self.root / ".urcapgen-cache"

    @property
    def sdk_root(self) -> Path:
        return self.cache / "ps5-sdk"

    def urcap_dir(self, uid: str) -> Path:
        d = self.urcaps / uid
        if not (d / "urcap.toml").is_file():
            raise RepoError(f"no URCap {uid!r} (no urcaps/{uid}/urcap.toml)")
        return d

    def urcap_ids(self) -> list[str]:
        if not self.urcaps.is_dir():
            return []
        return sorted(p.parent.name for p in self.urcaps.glob("*/urcap.toml"))


def find(start: Path) -> Repo:
    for d in [start.resolve(), *start.resolve().parents]:
        if (d / MARKER).is_file():
            return Repo(d)
    raise RepoError(f"not inside a URCap monorepo (no {MARKER} in {start} or above) — `urcapgen init <dir>` makes one")


# -- init / upgrade -----------------------------------------------------------------------------


def _subst(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def _template_files():
    """``(relative path, Traversable)`` for every file in the template, recursively."""

    def walk(node, prefix: str):
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                if child.name == "__pycache__":
                    continue
                yield from walk(child, rel + "/")
            else:
                yield rel, child

    seen = set()
    for rel, f in walk(TEMPLATE, ""):
        seen.add(rel)
        yield rel, f
    # The skill: in the template, or — in urcap-skill.zip, which may hold only one SKILL.md —
    # the skill folder this package ships in.
    skill = skill_source()
    for p in sorted(skill.rglob("*")):
        rel = f"{SKILL_DIR}/{p.relative_to(skill).as_posix()}"
        if p.is_file() and rel not in seen and "urcapgen" not in p.relative_to(skill).parts[:1]:
            yield rel, p


SKILL_DIR = ".claude/skills/urcap"


def skill_source() -> Path:
    """Where the skill's files are: the template's copy, else the folder around this package
    (the uploaded urcap-skill.zip: ``urcap/SKILL.md`` beside ``urcap/urcapgen/``)."""
    t = Path(str(TEMPLATE)) / SKILL_DIR
    if (t / "SKILL.md").is_file():
        return t
    up = Path(__file__).resolve().parent.parent
    if (up / "SKILL.md").is_file():
        return up
    raise RepoError("this urcapgen has no copy of its skill (SKILL.md) — reinstall it")


def _is_managed(rel: str) -> bool:
    if any(rel == d or rel.startswith(d + "/") for d in MANAGED_DIRS):
        return True
    return rel == "urcapgen" or bool(re.fullmatch(r"\.github/workflows/urcapgen-[^/]+\.yml", rel))


def _vendor(root: Path) -> None:
    """Copy this urcapgen package into ``root/tools/urcapgen`` (replacing what is there)."""
    src = Path(__file__).resolve().parent
    dest = root / "tools" / "urcapgen"
    if dest.resolve() == src:
        return  # running the vendored copy: it is already there
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # a vendored copy always carries the skill in its template, whichever way it was installed
    skill_dest = dest / "template" / SKILL_DIR
    if not (skill_dest / "SKILL.md").is_file():
        shutil.copytree(skill_source(), skill_dest, ignore=shutil.ignore_patterns("urcapgen", "__pycache__", "*.pyc"))


def _write_template(root: Path, values: dict[str, str], *, managed_only: bool) -> list[str]:
    written = []
    for rel, f in _template_files():
        rel = rel.replace("dot-", ".", 1) if rel.startswith("dot-") else rel.replace("/dot-", "/.")
        managed = _is_managed(rel)
        dest = root / rel
        if managed_only and not managed:
            continue
        if not managed and dest.exists():
            continue  # created once; yours now
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = f.read_bytes()
        try:
            data = _subst(data.decode("utf-8"), values).encode("utf-8")
        except UnicodeDecodeError:
            pass
        dest.write_bytes(data)
        if rel == "urcapgen":
            dest.chmod(0o755)
        written.append(rel)
    return written


def init(root: Path, name: str | None = None) -> Path:
    root = root.resolve()
    if (root / MARKER).exists():
        raise RepoError(f"{root} is already a URCap monorepo — `urcapgen upgrade` refreshes it")
    if root.exists() and any(p.name not in (".git",) for p in root.iterdir()) and not (root / ".git").exists():
        pass  # an existing project directory is fine; nothing of the user's is overwritten
    root.mkdir(parents=True, exist_ok=True)
    values = {"REPO_NAME": name or root.name, "URCAPGEN_VERSION": __version__}
    _write_template(root, values, managed_only=False)
    _vendor(root)
    (root / MARKER).write_text(json.dumps({"urcapgen": __version__}, indent=1) + "\n", encoding="utf-8")
    return root


def upgrade(repo: Repo) -> dict:
    old = json.loads((repo.root / MARKER).read_text(encoding="utf-8")).get("urcapgen")
    name = repo.root.name
    for managed in MANAGED_DIRS[1:]:
        d = repo.root / managed
        if d.exists():
            shutil.rmtree(d)
    for g in MANAGED_GLOBS:
        for p in repo.root.glob(g):
            p.unlink()
    written = _write_template(repo.root, {"REPO_NAME": name, "URCAPGEN_VERSION": __version__}, managed_only=True)
    _vendor(repo.root)
    (repo.root / MARKER).write_text(json.dumps({"urcapgen": __version__}, indent=1) + "\n", encoding="utf-8")
    return {"from": old, "to": __version__, "refreshed": written + ["tools/urcapgen/"]}


# -- a new URCap ---------------------------------------------------------------------------------


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def starter_spec(uid: str, name: str, vendor_id: str, vendor_name: str, java_package: str | None, parts: set[str]) -> str:
    unknown = parts - {"installation", "program", "toolbar", "daemon"}
    if unknown:
        raise RepoError(f"--parts: unknown {', '.join(sorted(unknown))} (installation, program, toolbar, daemon)")
    if "daemon" in parts:
        parts = parts | {"installation"}
    g = uid.replace("-", "_")
    out = [
        "# The one source of this URCap. `./urcapgen gen` writes ps5/ and psx/ from it;",
        "# the format is in .claude/skills/urcap/references/spec.md.",
        "",
        "[urcap]",
        f"id = {_toml_str(uid)}",
        f"name = {_toml_str(name)}",
        'version = "0.1.0"',
        f"description = {_toml_str(name + ' for Universal Robots e-Series (PolyScope 5) and PolyScope X.')}",
        "",
        "[vendor]",
        f"id = {_toml_str(vendor_id)}",
        f"name = {_toml_str(vendor_name)}",
    ]
    if java_package:
        out.append(f"java_package = {_toml_str(java_package)}")
    out += ["", "[compat]", 'ps5_floor = "5.4"', 'psx_floor = "10.8"']
    if "installation" in parts:
        out += [
            "",
            "# Installation tab (PolyScope 5) / Application (PolyScope X): settings every program shares.",
            "# Its script is the program's preamble, so it defines globals the program nodes use.",
            "[installation]",
            f"title = {_toml_str(name)}",
            'description = "Settings shared by every program that uses this URCap."',
            'script = """',
            f"global {g}_enabled = {{{{enabled}}}}",
            f"global {g}_speed = {{{{speed}}}}",
            '"""',
            "",
            "[[installation.fields]]",
            'key = "enabled"',
            'type = "bool"',
            'label = "Enabled"',
            "default = true",
            "",
            "[[installation.fields]]",
            'key = "speed"',
            'type = "float"',
            'label = "Speed"',
            'unit = "m/s"',
            "default = 0.25",
            "min = 0.01",
            "max = 1.0",
        ]
    if "program" in parts:
        guard = f"if {g}_enabled:\n" if "installation" in parts else ""
        out += [
            "",
            "# A program node (Program tab → URCaps / PolyScope X toolbox).",
            "[[program]]",
            'id = "say"',
            'title = "Say"',
            'description = "Shows a message in the log and waits."',
            'label = "{{message}}"',
            'script = """',
            *([f"if {g}_enabled:", "  textmsg({{message}})", "  sleep({{wait}})", "end"]
              if guard else ["textmsg({{message}})", "sleep({{wait}})"]),
            '"""',
            "",
            "[[program.fields]]",
            'key = "message"',
            'type = "string"',
            'label = "Message"',
            'default = "hello"',
            "",
            "[[program.fields]]",
            'key = "wait"',
            'type = "float"',
            'label = "Wait"',
            'unit = "s"',
            "default = 1.0",
            "min = 0",
            "max = 60",
        ]  # fmt: skip
    if "toolbar" in parts:
        out += [
            "",
            "# A toolbar button (PolyScope 5 header) / sidebar item (PolyScope X). Its content is",
            "# hand-written: ps5/src/.../ToolbarHooks.java and psx/<id>-frontend/toolbar.hooks.js.",
            "[toolbar]",
            f"title = {_toml_str(name)}",
        ]
    if "daemon" in parts:
        out += [
            "",
            "# A Python XML-RPC server: PolyScope 5 runs it on the controller (DaemonService),",
            "# PolyScope X in a backend container. URScript reaches it with rpc_factory().",
            "[daemon]",
            "port = 40405",
            'entry = "daemon/daemon.py"',
        ]
    return "\n".join(out) + "\n"


def new_urcap(repo: Repo, uid: str, *, name: str, vendor_id: str, vendor_name: str, java_package: str | None,
              parts: set[str]) -> Path:  # fmt: skip
    if not ID.fullmatch(uid):
        raise RepoError("a URCap id matches ^[a-z][a-z0-9-]*[a-z0-9]$ (it becomes the urcapID)")
    if not ID.fullmatch(vendor_id):
        raise RepoError("a vendor id matches ^[a-z][a-z0-9-]*[a-z0-9]$ (it becomes the vendorID)")
    d = repo.urcaps / uid
    if (d / "urcap.toml").exists():
        raise RepoError(f"urcaps/{uid}/urcap.toml already exists")
    d.mkdir(parents=True, exist_ok=True)
    (d / "urcap.toml").write_text(starter_spec(uid, name, vendor_id, vendor_name, java_package, parts), encoding="utf-8")
    try:
        load(d)
    except SpecError as exc:
        (d / "urcap.toml").unlink()
        raise RepoError(f"the starter spec does not load: {exc}") from None
    return d


# -- CI helpers ----------------------------------------------------------------------------------


def touched(repo: Repo, paths: list[str]) -> list[str]:
    """The URCaps ``paths`` (repo-relative) touch: all of them when the tooling or the
    workflows changed."""
    ids = repo.urcap_ids()
    for p in paths:
        if p.startswith("tools/urcapgen/") or re.match(r"\.github/workflows/urcapgen-", p) or p == MARKER:
            return ids
    hit = {p.split("/")[1] for p in paths if p.startswith("urcaps/") and p.count("/") >= 2}
    return [i for i in ids if i in hit]


def release_info(repo: Repo, tag: str) -> dict:
    m = re.fullmatch(r"(?P<id>[a-z][a-z0-9-]*[a-z0-9])-v(?P<version>.+)", tag)
    if not m or not SEMVER.fullmatch(m.group("version")):
        raise RepoError(f"tag {tag!r} is not <urcap-id>-v<major>.<minor>.<patch>")
    uid, version = m.group("id"), m.group("version")
    spec = load(repo.urcap_dir(uid))
    if spec.version != version:
        raise RepoError(f"tag {tag} but urcaps/{uid}/urcap.toml says version {spec.version}")
    return {"id": uid, "version": version, "name": spec.name, "ps5": spec.ps5_file, "psx": spec.psx_file,
            "magic": spec.magic_file, "platforms": list(spec.platforms)}  # fmt: skip

"""``urcapgen`` — the command line. Every command prints JSON or plain lines and exits non-zero
with a one-line reason on failure, so an agent or CI can act on it."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, generate, monorepo, parity, ps5, psx, scripttpl
from .spec import SpecError, load


class CliError(RuntimeError):
    pass


def _repo(args) -> monorepo.Repo:
    return monorepo.find(Path(args.repo) if args.repo else Path.cwd())


def _specs(repo: monorepo.Repo, ids: list[str]):
    ids = ids or repo.urcap_ids()
    if not ids:
        raise CliError("no URCaps under urcaps/ — `urcapgen new <id>` makes one")
    return [load(repo.urcap_dir(i)) for i in ids]


def cmd_init(args) -> int:
    root = monorepo.init(Path(args.dir), name=args.name)
    print(json.dumps({"monorepo": str(root), "urcapgen": __version__}))
    return 0


def cmd_upgrade(args) -> int:
    repo = _repo(args)
    print(json.dumps(monorepo.upgrade(repo)))
    return 0


def cmd_new(args) -> int:
    repo = _repo(args)
    d = monorepo.new_urcap(
        repo,
        args.id,
        name=args.name,
        vendor_id=args.vendor_id,
        vendor_name=args.vendor_name,
        java_package=args.java_package,
        parts=set(args.parts.split(",")) if args.parts else {"installation", "program"},
    )
    spec = load(d)
    p = generate.generate(spec)
    print(json.dumps({"urcap": str(d), "written": p["write"], "created": p["create"]}, indent=1))
    return 0


def cmd_gen(args) -> int:
    repo = _repo(args)
    bad = []
    for spec in _specs(repo, args.ids):
        p = generate.generate(spec, check=args.check)
        if args.check:
            stale = p["write"] + p["delete"] + p["create"]
            if stale:
                bad.append(f"{spec.id}: out of date with urcap.toml: {', '.join(stale)}")
            print(json.dumps({"urcap": spec.id, "stale": stale}))
        else:
            print(json.dumps({"urcap": spec.id, **{k: v for k, v in p.items() if k != "same"}}))
    if bad:
        for b in bad:
            print(b, file=sys.stderr)
        print("run `urcapgen gen` and commit the result", file=sys.stderr)
        return 1
    return 0


def cmd_list(args) -> int:
    repo = _repo(args)
    for i in repo.urcap_ids():
        try:
            s = load(repo.urcap_dir(i))
            print(json.dumps({"id": s.id, "name": s.name, "version": s.version, "platforms": list(s.platforms),
                              "nodes": [f"{n.kind}:{n.id}" for n in s.nodes], "daemon": bool(s.daemon)}))  # fmt: skip
        except SpecError as exc:
            print(json.dumps({"id": i, "error": str(exc)}))
    return 0


def cmd_validate(args) -> int:
    repo = _repo(args)
    for spec in _specs(repo, args.ids):
        print(json.dumps({"urcap": spec.id, "ok": True}))
    return 0


def cmd_render(args) -> int:
    repo = _repo(args)
    spec = load(repo.urcap_dir(args.id))
    node = next((n for n in spec.nodes if n.id == args.node), None)
    if node is None:
        raise CliError(f"{spec.id} has no node {args.node!r} (nodes: {', '.join(n.id for n in spec.nodes)})")
    values = {f.key: f.default for f in node.fields}
    kinds = {f.key: f.type for f in node.fields}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in kinds:
            raise CliError(f"{node.id} has no field {k!r}")
        values[k] = json.loads(v) if kinds[k] not in ("string", "choice") else v
    text = scripttpl.render(node.script_after if args.after else node.script, {k: (kinds[k], v) for k, v in values.items()})
    print("\n".join(scripttpl.lines(text)))
    return 0


def cmd_build(args) -> int:
    repo = _repo(args)
    out = Path(args.out) if args.out else repo.dist
    results = []
    for spec in _specs(repo, args.ids):
        if generate.plan(spec)["write"] or generate.plan(spec)["delete"]:
            raise CliError(f"{spec.id}: generated files are out of date — run `urcapgen gen {spec.id}` first")
        want = set(spec.platforms) if args.platform == "all" else {args.platform}
        r: dict = {"urcap": spec.id, "version": spec.version}
        if "ps5" in want and "ps5" in spec.platforms:
            jar = ps5.package(spec, out, repo.sdk_root)
            problems = ps5.install_checks(jar)
            if problems:
                raise CliError(f"{jar.name}: PolyScope 5 would refuse it: {'; '.join(problems)}")
            r["ps5"] = str(jar)
            r["magic"] = str(ps5.write_magic(spec, jar))
        if "psx" in want and "psx" in spec.platforms:
            from . import daemon

            licence = ps5._licence(spec)
            extra = daemon.psx_package_hook(spec, args) if spec.daemon else None
            r["psx"] = str(psx.package(spec.root / "psx", out, licence, extra))
        results.append(r)
        print(json.dumps(r))
    return 0


def cmd_sdk(args) -> int:
    repo = _repo(args)
    images = args.image
    if not images:
        floors = set()
        for spec in _specs(repo, []):
            if "ps5" in spec.platforms:
                props = ps5.read_properties((spec.root / "ps5" / "bundle.properties").read_text(encoding="utf-8"))
                plan = ps5.compat_plan(props)
                floors |= {plan["floor"], *plan["since"]}
        images = sorted(floors, key=ps5.version_key)
    for image in images:
        info = ps5.fetch_sdk(image, repo.sdk_root, source=args.source)
        print(json.dumps({k: info[k] for k in ("image", "api_version", "jars", "dir")}))
    return 0


def cmd_check(args) -> int:
    repo = _repo(args)
    spec = load(repo.urcap_dir(args.id))
    below = ps5.version_key(args.ps5) < ps5.version_key(spec.ps5_floor)
    if "ps5" not in spec.platforms or (below and args.skip_below_floor):
        why = "not built for PolyScope 5" if "ps5" not in spec.platforms else f"below the URCap's floor {spec.ps5_floor}"
        print(json.dumps({"urcap": spec.id, "version": args.ps5, "skipped": why}))
        return 0
    dist = Path(args.dist) if args.dist else repo.dist / spec.ps5_file
    if not dist.is_file():
        raise CliError(f"{dist} is not built — `urcapgen build {spec.id}` first")
    sdk_dir = ps5.ensure_sdk(args.ps5, repo.sdk_root)
    print(json.dumps(ps5.check(spec, sdk_dir, dist, args.ps5)))
    return 0


def cmd_parity(args) -> int:
    repo = _repo(args)
    failed = False
    for spec in _specs(repo, args.ids):
        res = parity.run(spec, repo.sdk_root, java=not args.no_java)
        print(json.dumps({"urcap": spec.id, "cases": res["cases"], "failures": len(res["failures"]), "skipped": res["skipped"]}))
        for f in res["failures"]:
            print(f"  {spec.id}: {f}", file=sys.stderr)
        failed |= bool(res["failures"])
    return 1 if failed else 0


def cmd_install(args) -> int:
    repo = _repo(args)
    spec = load(repo.urcap_dir(args.id))
    if args.ps5_container:
        print(json.dumps(ps5.install(repo.dist / spec.ps5_file, args.ps5_container)))
    if args.psx_host:
        res = psx.install(repo.dist / spec.psx_file, args.psx_host, args.port, replace=args.replace)
        print(json.dumps({k: v for k, v in res.items() if k != "payload"}))
        if not res["ok"]:
            return 1
    if not (args.ps5_container or args.psx_host):
        raise CliError("say where: --ps5-container NAME (an e-Series URSim) and/or --psx-host HOST")
    return 0


def cmd_changed(args) -> int:
    """The URCaps a change touches (CI's job list): every one when tooling or workflows changed."""
    repo = _repo(args)
    r = subprocess.run(
        ["git", "diff", "--name-only", f"{args.base}...HEAD"], cwd=repo.root, capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise CliError(f"git diff failed: {r.stderr.strip()}")
    paths = [p for p in r.stdout.splitlines() if p]
    print(json.dumps(monorepo.touched(repo, paths)))
    return 0


def cmd_release_info(args) -> int:
    """``<urcap-id>-v<version>`` → ``{id, version}``, checked against the spec."""
    repo = _repo(args)
    print(json.dumps(monorepo.release_info(repo, args.tag)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="urcapgen", description="One urcap.toml → a PolyScope 5 and a PolyScope X URCap.")
    ap.add_argument("--version", action="version", version=f"urcapgen {__version__}")
    ap.add_argument("--repo", help="the URCap monorepo (default: found from the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create a URCap monorepo (tooling, skill, CI) in DIR")
    p.add_argument("dir")
    p.add_argument("--name", help="the monorepo's title (default: the directory name)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("upgrade", help="refresh the monorepo's vendored urcapgen, skill and workflows from this urcapgen")
    p.set_defaults(fn=cmd_upgrade)

    p = sub.add_parser("new", help="add urcaps/<id>/urcap.toml and generate it")
    p.add_argument("id")
    p.add_argument("--name", required=True, help="the URCap's display name")
    p.add_argument("--vendor-id", required=True, help="lowercase, e.g. acme (PolyScope X vendorID)")
    p.add_argument("--vendor-name", required=True, help="e.g. 'Acme Automation'")
    p.add_argument("--java-package", help="default com.<vendor>.<urcap>")
    p.add_argument("--parts", help="comma list of installation,program,toolbar,daemon (default installation,program)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("gen", help="regenerate ps5/ and psx/ from urcap.toml (all URCaps by default)")
    p.add_argument("ids", nargs="*")
    p.add_argument("--check", action="store_true", help="write nothing; exit 1 if anything is out of date")
    p.set_defaults(fn=cmd_gen)

    p = sub.add_parser("list", help="the URCaps in this monorepo")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("validate", help="load and check urcap.toml")
    p.add_argument("ids", nargs="*")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("render", help="print a node's URScript (the reference renderer, no hooks)")
    p.add_argument("id")
    p.add_argument("node")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="a field value (JSON for numbers/bools/poses)")
    p.add_argument("--after", action="store_true", help="the script after the children")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("build", help="build .urcap (+ USB magic file) and .urcapx into dist/")
    p.add_argument("ids", nargs="*")
    p.add_argument("--platform", choices=("all", "ps5", "psx"), default="all")
    p.add_argument("--out", help="default <monorepo>/dist")
    p.add_argument("--backend-image", help="PolyScope X backend: use this already-built image tar instead of building")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("sdk", help="fetch PolyScope 5 URCap API jars (default: every floor in the monorepo)")
    p.add_argument("--image", action="append", help="a version tag like 5.4 or 5.12.8 (repeatable)")
    p.add_argument("--source", choices=("registry", "docker"), default="registry")
    p.set_defaults(fn=cmd_sdk)

    p = sub.add_parser("check", help="does the built .urcap work with this PolyScope 5 version's API")
    p.add_argument("id")
    p.add_argument("--ps5", required=True, help="a PolyScope 5 image tag, e.g. 5.12.8")
    p.add_argument("--dist", help="the jar (default dist/<id>-ps5-<version>.urcap)")
    p.add_argument("--skip-below-floor", action="store_true", help="exit 0 (skipped) for a version below the URCap's floor")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("parity", help="do the PS5 and PSX URCaps write the same URScript")
    p.add_argument("ids", nargs="*")
    p.add_argument("--no-java", action="store_true", help="skip the PolyScope 5 side (no JDK)")
    p.set_defaults(fn=cmd_parity)

    p = sub.add_parser("install", help="install a built URCap into a simulator or robot")
    p.add_argument("id")
    p.add_argument("--ps5-container", help="an e-Series URSim container name")
    p.add_argument("--psx-host", help="a PolyScope X host (the sim: localhost)")
    p.add_argument("--port", type=int, default=8000, help="PolyScope X web port (sim 8000, robot 80)")
    p.add_argument("--replace", action="store_true", help="PolyScope X: delete an installed copy first")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("changed", help="JSON list of the URCaps a change touches (for CI)")
    p.add_argument("--base", default="origin/main")
    p.set_defaults(fn=cmd_changed)

    p = sub.add_parser("release-info", help="check a <id>-v<version> tag against its spec")
    p.add_argument("tag")
    p.set_defaults(fn=cmd_release_info)

    from . import e2e

    e2e.add_commands(sub)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (CliError, SpecError, ps5.Urcap5Error, psx.UrcapError, monorepo.RepoError, RuntimeError) as exc:
        print(f"urcapgen: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout.flush()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())

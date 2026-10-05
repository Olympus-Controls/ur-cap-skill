# CI, releases, upgrades

Push the monorepo to GitHub and it runs. There are no secrets to set: everything it pulls
(UR's simulator images, the URCap API out of them) is public.

## `URCaps` workflow (`.github/workflows/urcapgen-ci.yml`)

Runs on every PR, on pushes to `main`, and weekly. It covers the URCaps the change touches,
or all of them when `tools/urcapgen`, these workflows, or `.urcapgen` change.

| job | what it proves |
| --- | --- |
| static | every `urcap.toml` loads; generated `ps5/` + `psx/` are current (`gen --check`) |
| build | `.urcap` compiles against the floor PolyScope's API; `.urcapx` packages; **parity** passes. `dist/` is uploaded as an artifact |
| ps5-api | per PolyScope 5 minor from the floor: the code compiles against that API, and every import is exported |
| e2e-ps5 | per PolyScope 5 minor: URSim starts the bundle without exceptions, the nodes' URScript runs, the daemon answers |
| e2e-psx | per PolyScope X release: installs, every node renders, a field edit persists, the backend answers |
| **URCaps gate** | the single check to mark **required** in branch protection: green when nothing relevant changed |

Versions come from `./urcapgen matrix ps5|psx`, so new releases are added to urcapgen, not
to the workflow. A failing job uploads its simulator logs and screenshots as artifacts.

Set it up once: Settings → Branches → protect `main` → require **URCaps gate**.

## Releasing

1. Bump `version` in `urcaps/<id>/urcap.toml`, `./urcapgen gen <id>`, commit, merge to `main`
   (the gate runs everything).
2. `git tag <id>-v<version> && git push origin <id>-v<version>`.
3. The `URCap release` workflow (`urcapgen-release.yml`) checks the tag matches the spec,
   rebuilds, re-runs parity, and publishes a GitHub Release. It attaches `<id>-ps5-<v>.urcap`,
   `urmagic_<id>.sh`, `<id>-<v>.urcapx` and `SHA256SUMS`, with install instructions and the
   change list since the last tag of that URCap.

Each URCap has its own tag line. Tags of other URCaps don't interfere.

## When UR ships a new PolyScope

The `UR releases` workflow (`urcapgen-track.yml`) checks Docker Hub weekly. When there's a
release urcapgen doesn't test yet, it opens an issue labelled `urcapgen-attention`. Fix:
`./urcapgen upgrade` from a newer urcapgen, or add the release to `tools/urcapgen/e2e.py`'s
tables upstream.

## Upgrading urcapgen

`./urcapgen upgrade`, run from the newer urcapgen (e.g.
`python3 -m urcapgen --repo . upgrade` in a checkout of it, or `<newer>/urcapgen upgrade`).
It replaces `tools/urcapgen/`, `.claude/skills/urcap/` and the `urcapgen-*.yml` workflows;
it never touches `urcaps/`, `AGENTS.md`, `README.md` or `.gitignore`. Then run
`./urcapgen gen` (the runtime files change with the version) and commit everything together.

## Running CI's steps locally

| | needs |
| --- | --- |
| `./urcapgen gen --check && ./urcapgen parity` | Python 3.11+, Node 18+, a JDK 11+ (or Docker) |
| `./urcapgen build` | the same, plus Docker for a daemon backend |
| `./urcapgen e2e-ps5 <id> --version 5.x` | amd64 Linux with Docker (not Apple silicon) |
| `./urcapgen e2e-psx <id> --version 10.x` | Docker + `pip install playwright==1.63.0 && playwright install chromium` |

Point `URCAPGEN_JDK` at a JDK home, or set `URCAPGEN_JDK=docker` to use `eclipse-temurin:21-jdk`.

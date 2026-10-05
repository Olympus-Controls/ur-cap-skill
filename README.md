# ur-cap-skill: urcapgen

**One `urcap.toml` gives you a Universal Robots URCap for PolyScope 5 (e-Series) *and*
PolyScope X, built, tested against every PolyScope release, and released by GitHub Actions.**
It comes with an assistant skill, so you can describe a URCap and have it built.

```toml
[urcap]
id = "acme-gripper"
name = "Acme Gripper"
version = "0.1.0"

[vendor]
id = "acme"
name = "Acme Automation"

[installation]                      # Installation tab (PS5) / Application (PSX)
script = """
global acme_gripper_force = {{force}}
"""
[[installation.fields]]
key = "force"
type = "float"
unit = "N"
default = 50
min = 20
max = 185

[[program]]                         # a program node on both platforms
id = "grip"
title = "Grip"
label = "{{width}} mm"
script = """
textmsg("grip ", {{width}})
set_tool_digital_out(0, True)
"""
[[program.fields]]
key = "width"
type = "float"
default = 40
min = 0
max = 85
```

`urcapgen gen` writes the PolyScope 5 Java/Swing bundle sources and the PolyScope X web
components from that file. `urcapgen build` produces `acme-gripper-ps5-0.1.0.urcap` (plus a
USB auto-install script) and `acme-gripper-0.1.0.urcapx`. Both render the node's URScript
from the same template at runtime, and `urcapgen parity` proves they write identical lines.

## What you get

- **Program nodes, installation/application nodes, toolbar/sidebar items, and a daemon**
  (Python XML-RPC: run by PolyScope 5's DaemonService, and as a PolyScope X backend
  container), on both platforms from one spec.
- **Generated forms** for int, float, bool, string, choice and pose fields. They use the
  pendant keypad on PS5, and get "Set position" from the arm.
- **Hooks** for anything the spec can't say (custom UI, computed scripts), with parity
  enforced between the Java and JS versions.
- **No Maven, no npm.** Stdlib Python, plus a JDK (or Docker) and Node. The PS5 API jars
  come straight out of UR's public URSim images; nothing behind a login.
- **PS5 builds against the oldest PolyScope you support** (5.4 by default), so a newer API
  call fails the build, not a customer's pendant.
- **A URCap monorepo** with CI that checks every URCap against every PolyScope 5 minor and
  PolyScope X release from its floor (API check + simulator e2e), a single required gate,
  tag-to-GitHub-Release CD, and a weekly watch for new UR releases.

## Use it

```bash
git clone https://github.com/Olympus-Controls/ur-cap-skill && cd ur-cap-skill
./scripts/install-local.sh            # `urcapgen` on PATH + the skill in ~/.claude/skills (live)

urcapgen init ~/github/acme-urcaps --name "Acme URCaps"
cd ~/github/acme-urcaps && git init -b main
./urcapgen new acme-gripper --name "Acme Gripper" --vendor-id acme --vendor-name "Acme Automation" \
    --parts installation,program,toolbar,daemon
./urcapgen parity && ./urcapgen build
```

Or `pip install git+https://github.com/Olympus-Controls/ur-cap-skill` for just the `urcapgen` command.

With Claude Code, ask in any directory: *"make a URCap that …"*. The `urcap` skill
bootstraps a monorepo if there isn't one, asks what it can't infer (vendor identity, I/O,
units), writes the spec, generates, verifies and commits. Inside a monorepo the skill is
vendored at `.claude/skills/urcap/`, so Claude Code on the web picks it up too.

The skill's knowledge is plain markdown (`SKILL.md` + `references/`), and `AGENTS.md` points
any assistant at it. Nothing in it is Claude-specific except the skill frontmatter.

## Layout of this repository

| | |
| --- | --- |
| `urcapgen/spec.py` | `urcap.toml`: validation and every derived name |
| `urcapgen/scripttpl.py` | the URScript template language: the reference implementation |
| `urcapgen/runtime/` | what ships inside every URCap: `ScriptTemplate.java` + `Form.java` (PS5), `urcapgen-runtime.js` (PSX), the USB magic-file template |
| `urcapgen/generate.py`, `daemon.py` | spec → `ps5/` + `psx/` (+ daemon) |
| `urcapgen/ps5.py`, `psx.py` | build, check, install (`.urcap` / `.urcapx`) |
| `urcapgen/parity.py` | both URCaps' scripts vs each other and the reference |
| `urcapgen/e2e.py` | simulator matrices and end-to-end tests |
| `urcapgen/monorepo.py`, `cli.py` | `init` / `upgrade` / `new`, the command line |
| `urcapgen/template/` | what `init` writes: the wrapper, the skill, the workflows, AGENTS.md |
| `tests/` | `python3 -m unittest discover -s tests -t .` |

Developing urcapgen: [AGENTS.md](AGENTS.md).

MIT licensed. URCaps you generate are yours. UR's URCap API jars are downloaded at build
time and never redistributed.

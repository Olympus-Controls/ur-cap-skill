# {{REPO_NAME}}

URCaps for Universal Robots, each built for **PolyScope 5** (e-Series) and **PolyScope X**
from one `urcap.toml`, with [urcapgen](tools/urcapgen) {{URCAPGEN_VERSION}}.

| URCap | |
| --- | --- |
| *(none yet)* | `./urcapgen new <id> --name "…" --vendor-id … --vendor-name "…"` |

## Install a URCap on a robot

Download it from this repository's **Releases**:

- **PolyScope 5:** copy `<id>-ps5-<version>.urcap` to a FAT32 USB stick → ☰ Settings →
  System → URCaps → **+** → the file → Open → Restart. Or also copy `urmagic_<id>.sh` to
  the stick: with Settings → Security → General → *Run magic files* enabled, the robot
  installs it by itself.
- **PolyScope X:** ☰ → System Manager → URCaps → add `<id>-<version>.urcapx` from a USB stick.

## Work on one

```bash
./urcapgen list                       # what is here
./urcapgen gen && ./urcapgen parity   # regenerate, check both platforms write the same URScript
./urcapgen build                      # dist/: .urcap, urmagic_*.sh, .urcapx
```

Needs Python 3.11+, Node 18+, and a JDK 11+ (or Docker). CI (`.github/workflows/`) builds
and tests every URCap against every PolyScope release from its floor. Tag `<id>-v<version>`
to publish a release. Details: [.claude/skills/urcap/references/ci.md](.claude/skills/urcap/references/ci.md).

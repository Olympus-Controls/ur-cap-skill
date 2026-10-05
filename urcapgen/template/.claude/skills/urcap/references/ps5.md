# PolyScope 5 (e-Series)

## What is generated

`ps5/bundle.properties` + `ps5/src/<java package>/`:

| file | role |
| --- | --- |
| `Activator.java` | registers every service with OSGi |
| `<Node>Service.java` | what PolyScope asks for: id, title, children allowed, factory |
| `<Node>View.java` | the Swing screen: the generated `Form` + your `extendUI` |
| `<Node>Contribution.java` | the node's state (DataModel), title, `isDefined`, `generateScript` |
| `<Node>Spec.java` | the node's fields and templates as constants |
| `Form.java`, `ScriptTemplate.java` | shared runtime: pendant keypad inputs, the template engine |
| `ToolbarService/Contribution.java`, `Icons.java` | header button + popup (`[toolbar]`) |
| `Daemon.java` | `DaemonService` (`[daemon]`) |

## How it's built (no Maven)

`./urcapgen build` compiles with `javac --release 8 -Xlint:all -Werror` against UR's URCap
API jars. Those come out of the `universalrobots/ursim_e-series:<floor>` image on Docker Hub
(streamed from the registry, no Docker needed) into `.urcapgen-cache/ps5-sdk/<version>/`.
They're UR's: never commit them. `jdeps` derives `Import-Package`; the URCap API gets the
range `[1.0.0,2.0.0)`.

The jar's manifest carries `Bundle-Category: URCap` and both `URCapCompatibility-*` flags
(PolyScope's installer refuses a jar without them). It embeds
`META-INF/maven/<group>/<artifact>/pom.xml` naming `com.ur.urcap:api` at the **floor's**
version (5.4 → 1.7.0). PolyScope 5.10+ refuses a URCap whose pom names a newer API than it
has. The build is reproducible, and `META-INF/urcapgen-sources.sha256` records the sources.

Compiling against the floor means **any API newer than the floor fails the build**. That's
deliberate: it fails here instead of with `NoSuchMethodError` on a customer's pendant.

## Using newer API than the floor

Raise `compat.ps5_floor` if every customer is on a newer PolyScope. Otherwise, isolate the
new-API code:

1. Put it in its own class (e.g. `TeachV2.java`), referenced from nowhere except by name:
   `Class.forName("<pkg>.TeachV2")` behind a check that the newer API class exists.
2. List it under `compat.since.<version>` in `bundle.properties`. Today that's a hand edit
   of a generated file, so ask the user before doing it. It gets compiled against that
   version's jars.
3. List the packages only it imports in `compat.optional-packages`.

## Platform facts

- **Java 8 runtime** (1.8.0_371). No `var`, no records, no `List.of`.
- **Swing on a 1280×800 touch pendant.** Inputs need the pendant keypad
  (`KeyboardInputFactory`); `Form` does this for spec fields. Big touch targets: 36 px+ high.
- **Undo:** a program node's DataModel changes must happen inside
  `getUndoRedoManager().recordChanges(...)`. `Form` does this through `Store.change`.
- **Titles:** `getTitle()` is the tree text (`Title: label`); `isDefined()` turns the node
  yellow when false, and the program won't run.
- **Installation preamble:** `InstallationNodeContribution.generateScript` output starts
  every program. Program nodes' code goes where the node sits; `writer.writeChildren()`
  places a container's children.
- **Poses:** "Set position" opens PolyScope's move screen and stores the **TCP pose in base**
  (m, rad) under the active TCP.
- **Saved programs** store the node's DataModel. Changing a field's key or type breaks
  them, and changing the service id (`getId`) orphans them.

## The daemon on PolyScope 5

- The jar carries `daemon/` at `<package path>/daemon/`. `Daemon.init` calls
  `installResource`, and PolyScope extracts it, **sets the executable bit itself**, and runs
  the entry under **runit** (`/etc/service`, logs via svlogd).
- The entry needs a **shebang** (`#!/usr/bin/env python`) and **LF line endings**, and must
  stay in the foreground.
- **Python:** only **2.7** is certain on real control boxes (UR's own sample daemon is
  Python 2; python3 varies). URSim images have both. Keep `daemon.py` 2.7- *and*
  3-compatible: no f-strings, no type hints, `from __future__ import print_function`,
  stdlib only.
- The installation node's constructor starts it. URScript reaches it at
  `http://127.0.0.1:<port>/RPC2`, through the preamble's `<id>_daemon` global.
- It runs as root on the controller. Treat input as untrusted, bind 127.0.0.1 (the
  default), and never shell out with user strings.

## Installing

- **USB:** Settings → System → URCaps → **+** → the `.urcap` → Restart. Or put
  `urmagic_<id>.sh` beside it on the stick. With Settings → Security → General → **Run magic
  files** on, the robot installs it when the stick goes in, and restarts if the arm is off
  and no program runs.
- **Simulator:** `./urcapgen install <id> --ps5-container <name>` copies to `/urcaps` and
  restarts.
- **SSH (5.10+, root/`easybot` by default):** copy the jar and magic script to `/tmp`, run
  the script.

## Simulator traps

- `ursim_e-series` is **amd64-only** and doesn't run under Docker Desktop's emulation on Apple
  silicon. Use an amd64 Linux host or CI.
- Running needs `security_opt: seccomp:unconfined` (URControl dies with `socket() ENOSYS`
  otherwise).
- On some hosts (Docker 29, WSL2) URControl also needs `--ulimit memlock=-1:-1`; otherwise
  `URControl.log` says `Cannot spawn thread. Error code: 11` and the Dashboard reports
  `NO_CONTROLLER`. `./urcapgen e2e-ps5` sets both.
- Primary-port URScript (`30001`) is auto-wrapped. Send one `def …(): … end` block.
- A real robot gates Primary *motion* on **Remote** mode; URSim doesn't.

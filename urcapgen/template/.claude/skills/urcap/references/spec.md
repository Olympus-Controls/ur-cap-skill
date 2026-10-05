# `urcap.toml` reference

One file per URCap at `urcaps/<id>/urcap.toml`. TOML; `./urcapgen validate` checks every
rule below and names the offending key.

## `[urcap]` (required)

| key | | |
| --- | --- | --- |
| `id` | required | `^[a-z][a-z0-9-]*[a-z0-9]$`. Becomes PolyScope X's `urcapID`, file names, the tag prefix. **Permanent.** |
| `name` | | display name (default: `id`) |
| `version` | required | `major.minor.patch`. Bump it for every release; the release tag must match it |
| `description` | | one sentence; shown in PolyScope's URCap lists |

## `[vendor]` (required)

| key | | |
| --- | --- | --- |
| `id` | required | `^[a-z][a-z0-9-]*[a-z0-9]$`, PolyScope X `vendorID`. **Permanent.** |
| `name` | required | shown as the vendor |
| `java_package` | | default `com.<vendor id>.<urcap id>` without dashes. The PS5 bundle's symbolic name and package. **Permanent.** |

## `[compat]`

| key | default | |
| --- | --- | --- |
| `ps5_floor` | `"5.4"` | oldest PolyScope 5 supported. The jar is compiled against **that** release's API, so a call it lacks fails the build. urcapgen's minimum is 5.4 |
| `psx_floor` | `"10.8"` | oldest PolyScope X supported. A `[toolbar]` raises it to `10.10` (sidebar items are unverified before that) |
| `platforms` | `["ps5", "psx"]` | build only one platform by dropping the other |

## `[installation]`: zero or one

PolyScope 5: an Installation tab → URCaps node. PolyScope X: an Application node. Its
`script` is the **preamble** of every program run with the URCap installed, so it defines the
`global`s and `def`s the program nodes use. It runs before any program node's script.

| key | |
| --- | --- |
| `title`, `description` | the screen's heading and text |
| `script` | URScript template (below) |
| `[[installation.fields]]` | its settings (below) |

## `[[program]]`: zero or more

A node in the program tree (PolyScope 5 Program tab → URCaps; PolyScope X toolbox).

| key | |
| --- | --- |
| `id` | required, `^[a-z][a-z0-9]*(_[a-z0-9]+)*$`. **Permanent once released.** Saved programs find the node by it (PS5 service id `CamelCase(id)`, PSX tag `<vendor>-<urcap>-<id>`) |
| `title` | required; the node's name in the tree and toolbox |
| `description` | text on the node's screen |
| `label` | template for the tree row's detail (`"{{force}} N"`). Strings render **without** quotes here |
| `script` | URScript template: the node's code (before its children, if any) |
| `allows_children` | `true`: the node is a container. Children's code goes between `script` and `script_after` |
| `script_after` | URScript template after the children (needs `allows_children`) |
| `[[program.fields]]` | the node's parameters |

## `[toolbar]`: zero or one

PolyScope 5: a button in the header with a popup. PolyScope X: a sidebar item. The content
is hand-written (`ToolbarHooks.java`, `toolbar.hooks.js`); the spec gives `title` and
`description` only. It writes no URScript.

## `[daemon]`: zero or one (needs `[installation]`)

A Python XML-RPC server (`daemon/daemon.py`, yours) for things URScript can't do itself:
serial devices, network protocols, computation. PolyScope 5 runs it on the controller,
PolyScope X in a backend container. See `ps5.md` and `psx.md`.

| key | default | |
| --- | --- | --- |
| `port` | `40405` | the port it listens on |
| `entry` | `"daemon/daemon.py"` | the script, relative to the URCap directory |

The installation preamble starts with `global <urcap_id>_daemon = rpc_factory("xmlrpc", …)`
(dashes → underscores), pointing at the right address per platform. Call it from any
template: `<urcap_id>_daemon.ping()`. The URL is generated; never write one yourself.

## Fields

```toml
[[program.fields]]
key = "force"        # required, ^[a-z][a-z0-9_]*$, not a Java keyword. Permanent once released.
type = "float"       # int | float | bool | string | choice | pose
label = "Grip force" # default: the key, capitalised
unit = "N"           # shown after the label
help = "Hand-E: 20–185 N"  # shown under the input
default = 50         # see the table
min = 20             # int/float only, enforced on both platforms
max = 185
```

| type | default if omitted | renders as | notes |
| --- | --- | --- | --- |
| `int` | `min` if > 0, else 0 | `7` | |
| `float` | `min` if > 0, else 0 | `0.25`, `1.0`: at most 6 decimals, rounded half away from zero, always a `.` | |
| `bool` | `false` | `True` / `False` | |
| `string` | `""` | `"text"` with quotes (no quotes in a `label`) | no `"` or line breaks; the UI strips them |
| `choice` | the first option | the option's **value**, verbatim | `options = ["a", {value = "2", label = "Two"}]`. A value is any one-line URScript literal (`2`, `"fast"`, `p[0,0,0.1,0,0,0]`) |
| `pose` | `[0,0,0,0,0,0]` | `p[x, y, z, rx, ry, rz]` | metres and radians, base frame. The UI offers "Set position" (PS5 move screen; PSX 10.10+ current TCP) |

Fields are stored per node (PS5 DataModel, PSX node parameters). A node saved before a field
existed gets the field's default.

## Templates

`script`, `script_after` and `label` use a small Mustache subset, rendered **at runtime** by
the URCap from the node's current values (identically in Java and JS):

| | |
| --- | --- |
| `{{key}}` | the field as a URScript literal (table above) |
| `{{#key}}…{{/key}}` | when truthy: `true`, non-zero, non-empty. A pose is always truthy |
| `{{^key}}…{{/key}}` | when falsy |
| `{{#key=value}}…{{/key}}` | when the raw value equals `value` (choice/string: the value; bool: `true`/`false`; numbers: as rendered) |
| `{{^key=value}}…{{/key}}` | when it doesn't |
| `{{! comment }}` | nothing |

A line holding only a section tag or comment disappears completely. Indentation and blank
lines don't matter: both platforms strip them. A template may use only its own node's
fields. Share installation settings through the preamble's globals:

```toml
[installation]
script = """
global gripper_ip = {{ip}}
"""
[[installation.fields]]
key = "ip"
type = "string"
default = "192.168.1.20"

[[program]]   # with fields width (float), mode (choice: fast/slow), wait (bool), settle (float)
id = "grip"
title = "Grip"
label = "{{width}} mm{{#wait}}, wait{{/wait}}"
script = """
textmsg("grip at ", gripper_ip)
{{#mode=fast}}
set_tool_digital_out(0, True)
{{/mode}}
{{^mode=fast}}
set_tool_digital_out(1, True)
{{/mode}}
{{#wait}}
sleep({{settle}})
{{/wait}}
"""
```

`./urcapgen render <id> grip --set mode=slow --set wait=true` shows the result.

# URScript in templates and hooks

A node's code is spliced into the program PolyScope generates. The installation preamble
comes first, then the program's nodes in tree order. Write it as statements, not as a
program.

## Rules

- **Indentation is cosmetic; blocks end with `end`.** `if x:` … `end`, `while x:` … `end`,
  `def f():` … `end`. Both platforms strip indentation and blank lines.
- **Globals are how nodes share state.** The installation preamble declares them
  (`global gripper_ip = "…"`); program nodes read them. Name them `<urcap_id>_<name>`, with
  underscores, so two URCaps never collide.
- **Define functions in the preamble**, not in program nodes. A node can appear many times
  in one program, and two identical `def`s are a compile error.
- **No nested `def`s.** Only one level.
- **`local` is function-scoped.** It's hoisted to the enclosing function. Assigning a name
  without `local` inside a function writes the global.
- **No resizable arrays, no `random()`.** Lists are fixed-size literals. Keep a separate
  counter for "used length".
- **Strings:** `str_cat(a, b)` takes exactly two arguments (chain it). `to_str(x)` converts
  anything; there's no `format()`. `textmsg(a, b)` takes at most two arguments.
- **Tight loops need `sync()`** (or a motion or `sleep`) every iteration, or the controller
  stops the program for non-real-time execution.
- **`request_*_from_primary_client` blocks** until a Primary client answers. Never use it in
  a node.
- **Poses:** `p[x, y, z, rx, ry, rz]`: metres, rotation vector in radians, base frame.
  Compose with `pose_trans(a, b)`, not `pose_add`, unless you mean component-wise addition.
- **Motion from a node:** prefer `movej` to reach a dexterous configuration, then `movel`.
  `movel` through a singularity trips a protective stop (C154A0). Always give `a=` and `v=`
  explicitly, within what the cell's risk assessment allows. **Never invent speeds or
  positions.** Ask.
- **I/O:** `set_standard_digital_out(n, b)`, `set_tool_digital_out(n, b)`,
  `get_standard_digital_in(n)`. Get the real numbers from the user.
- **XML-RPC** (the daemon): `<id>_daemon.method(args)`. Ints, floats, bools, strings, lists and
  poses (a pose arrives as a struct `{x, y, z, rx, ry, rz}`). Calls block, so keep daemon
  methods fast or make them asynchronous (start, then poll).

## Checking a script

`./urcapgen render <id> <node> --set key=value …` prints exactly what the reference renderer
writes (hooks not applied). `./urcapgen e2e-ps5` runs the preamble plus every node's default
script on a URSim controller and fails on compile or runtime errors.

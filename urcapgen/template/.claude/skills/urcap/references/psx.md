# PolyScope X

## What is generated

`psx/manifest.yaml` + `psx/<id>-frontend/` (a web archive). Plain JavaScript web components,
**no Angular, no webpack, no npm**:

| file | role |
| --- | --- |
| `contribution.json` | `applicationNodes` (installation), `programNodes`, `sidebarItems` (toolbar) |
| `<tag>.js` | presenter: a custom element. Program nodes get a one-line tree row plus a dialog with the form |
| `<tag>.worker.js` | behavior worker: `factory`, `upgradeNode`, `validator`, `programNodeLabel`, `generateCodeBeforeChildren/AfterChildren`, `generatePreamble` |
| `urcapgen-runtime.js` | shared: the template engine, the form, the threads.js worker protocol |
| `spec.js` | the URCap's fields and templates (`self.UrcapSpec`) |
| `assets/i18n/en.json`, `assets/icons/*.svg` | titles, icons |
| `<id>-backend/Dockerfile` | the backend container (`[daemon]`) |

Tags: application `<vendor>-<urcap>`, program `<vendor>-<urcap>-<node-id>`, sidebar
`<vendor>-<urcap>-toolbar`.

## How it's built

`.urcapx` is a gzipped tar with `manifest.yaml` first. The build is reproducible: fixed
owners, modes and mtime. With a daemon, `docker build --platform linux/amd64` + `docker save`
puts `<id>-backend/<id>-backend.tar` in the archive, and the manifest names the image
`<id>-backend:latest`. **Robots run amd64 images only.**

## Platform facts (SDK 6.0.27 / PolyScope X 10.8 through SDK 6.6.66 / 10.14)

- **Behavior workers speak threads.js 1.7 directly.** `registerXBehavior(b)` is `expose(b)`:
  messages `init`, `run`, `running`, `result`, `error`. `urcapgen-runtime.js` implements it,
  so nothing is bundled.
- **A ScriptBuilder crosses the worker boundary** as
  `{type: "$$ScriptBuilder", script, currentIndent}`. Empty lines are dropped.
- **A program node's presenter renders inside its 48 px tree row.** The real screen is a
  dialog: `presenterAPI.dialogService.openCustomDialog(tag, inputData, {title, dialogSize: "XL", confirmText})`.
  It saves through `programNodeService.updateNode(node)` as it goes.
- **The application node** saves through `applicationAPI.applicationNodeService.updateNode(node)`.
- **Poses** ("Use the arm's current position") use `robotPositionService.getJointPositions()`
  plus `convertJointPositionsToTcpPose` (**10.10+**). On 10.8–10.9 the user types the numbers.
- **10.8 differences:** `onLifeCycleHook` gets the node itself (10.9+: `{id, node}`), and there
  is no `disabled`. Sidebar items are unverified before 10.10, which is why `[toolbar]`
  raises the floor.
- **No frontend API runs URScript.** Scripts come from behaviors only.
- **Text inputs** are plain `<input>`; PolyScope X's on-screen keyboard handles them, and the
  dialog opens with `raiseForKeyboard`.

## The backend container (`[daemon]`)

- `manifest.yaml` declares
  `containers: [{id: <id>-backend, image: <id>-backend:latest, ingress: [{id: xmlrpc, containerPort: <port>, proxyUrl: /}]}]`.
- **URScript** reaches it at `http://servicegateway/<vendor>/<urcap>/<id>-backend/xmlrpc/`,
  through the preamble's `<id>_daemon` global. The ingress strips the prefix, so the server
  serves `/` (and `/RPC2` for PolyScope 5).
- **A presenter** reaches it at
  `${location.protocol}//` + `api.getContainerContributionURL(vendor, urcap, "<id>-backend", "xmlrpc")`.
- **Python 3** (`python:3.12-alpine`). The same `daemon.py` runs on PolyScope 5's 2.7, so keep
  it compatible.
- **Hardware:** the manifest can grant `devices` (`ttyTool`, `serial`, `video`, `network`) and
  `services` (`urcontrol-primary` → `urcontrol-primary:30001`, `urcontrol-rtde`, …).
  urcapgen doesn't emit these yet. Ask before hand-adding them; the manifest is generated.

## Installing

- **Robot:** System Manager → URCaps → add the `.urcapx` from USB. Or over the network:
  `./urcapgen install <id> --psx-host <robot> --port 80 --replace`.
- **Simulator:** `--psx-host localhost --port 8000`. This uses PolyScope's own urservice
  endpoint (`POST /universal-robots/urservice/api/v1/urcaps`, field `urcapxFile`), which works
  in Local mode. UR's SDK CLI uses the Robot-API instead, which answers **403 unless in Remote
  mode**. There is no update verb: `--replace` deletes, then installs.
- Refresh the PolyScope page after installing.

## Simulator

`universalrobots/ursim_polyscopex:<tag>` is published for amd64 and arm64. The release ↔ image
tag table is in `tools/urcapgen/e2e.py`. `./urcapgen e2e-psx <id> --version 10.x` boots it,
installs, and clicks through every node in headless Chromium (Playwright).

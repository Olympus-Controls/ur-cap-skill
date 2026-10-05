# URCap Skill for Claude

Describe the URCap you want in plain words. Claude builds it for **PolyScope 5 (e-Series)**
and **PolyScope X**, tests it on every software version Universal Robots has released, and
hands you files to put on a USB stick.

**Simple step-by-step guide: https://olympus-controls.github.io/ur-cap-skill/**

## What you need

| | |
| --- | --- |
| **Claude** | A paid plan (Pro, Max, Team or Enterprise), using the Claude Desktop app for Windows or claude.ai |
| **GitHub** | A free account at [github.com](https://github.com). This is where your URCaps get built, tested and stored |
| **Robot** | An e-Series arm on PolyScope 5.4 or newer, or any arm on PolyScope X 10.8 or newer. CB3 robots are not supported |
| **USB stick** | FAT32 (most are), to carry the file to the robot |

Nothing to install on your computer.

## Set up (once)

1. **Download the skill:** [urcap-skill.zip](https://github.com/Olympus-Controls/ur-cap-skill/releases/latest/download/urcap-skill.zip). Don't unzip it.
2. **Add it to Claude:** in Claude's Settings, make sure **Code execution and file creation**
   is turned on. Then Settings → Customize → Skills → **Upload skill** → choose the zip.
3. **Make a home for your URCaps on GitHub:** github.com → **New repository** → give it a
   name (e.g. `my-urcaps`) → tick **Add a README file** → **Create repository**.
4. **Connect it:** in Claude, open **Code**, connect your GitHub account when asked, and
   pick the repository you just made.

## Make a URCap

In that Code session, just say what you want. For example:

> Make a URCap called **Part Counter** for our cells. Add a program node **Count Part**
> that adds one to a part counter and pops up a message every 100 parts. In the
> installation screen, let me set the popup interval and reset the count.

> Make a URCap **Vacuum Gripper** with two program nodes, **Pick** and **Release**. Pick
> turns on tool digital output 0, waits until standard digital input 3 is on (vacuum OK),
> and stops with a popup if it doesn't come on within a timeout I can set in the node,
> default 2 seconds. Release turns output 0 off and pulses output 1 for 0.3 s.

> Add a toolbar button to Part Counter that shows the current count.

Claude asks about anything it can't work out, like your company name, which I/O to use,
units and limits. **It never guesses I/O numbers or motion values.** It then builds the URCap
and saves it to GitHub.

## Get it on the robot

1. GitHub tests the URCap in Universal Robots' own simulator, on **every PolyScope 5 version
   from 5.4 and every PolyScope X release**. That takes about 20–40 minutes. Watch it on your
   repository's **Actions** tab: a green tick means it passed everywhere.
2. Tell Claude: **"Release it."** A minute later the files are on your repository's
   **Releases** page.
3. Install:
   - **PolyScope 5:** copy the `.urcap` file to the USB stick → on the pendant ☰ →
     Settings → System → URCaps → **+** → pick the file → **Restart**.
     *Shortcut:* also copy the `urmagic_….sh` file to the stick and turn on Settings →
     Security → General → **Run magic files**. The robot then installs it by itself when
     the stick goes in (arm powered off).
   - **PolyScope X:** copy the `.urcapx` file to the USB stick → ☰ → System Manager →
     URCaps → add it.

To change a URCap later, open the same repository in Claude's Code tab and say what to
change ("make the default timeout 3 seconds", "add a node that…"). Then release again.

## Good to know

- **Private repositories work.** GitHub gives free accounts 2,000 test minutes a month on
  private repositories, and a full test run uses a good part of that. Public repositories
  test for free without limit.
- **Your URCaps belong to you.** The URCaps you make are yours. This skill is MIT licensed.
- **Test before production.** Simulator tests catch broken URCaps, not unsafe programs.
  Try every new URCap on a real robot with reduced speed before production, as you would
  any program.
- Without GitHub, Claude can still design a URCap in a normal chat and give you the files
  as a zip, but it can't build or test them there.

---

Developers: [AGENTS.md](AGENTS.md) explains how urcapgen works and how to run it from the
command line.

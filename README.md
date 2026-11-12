# Resolve AI Bridge

A local bridge between an AI agent and **DaVinci Resolve Studio**: a Python
library plus a stdio [MCP](https://modelcontextprotocol.io) server that exposes
25 bounded tools. An agent can read a project, plan an edit, and execute it with
verification. It cannot run scripts, Lua, expressions or arbitrary setters,
because no such tool exists.

- No third-party dependencies. It runs in whatever Python can load Resolve's own
  scripting module.
- Every write is allowlisted, journaled, idempotent and re-read after the fact.
- Out of the box it can touch nothing: the allowlist starts empty.

Status: alpha. Developed and live-tested on Resolve Studio **21.0.0.48**, Python
3.13, macOS arm64. Windows and Linux paths exist but are untested. See
[Build pin](#resolve-build-pin).

## What this repository is

A rough starting point, not a supported product. It is one editor's working
setup, cleaned up so another editor's agent can judge whether it fits their
machine and adapt it. Expect to change things.

### If you are an agent asked "can I use this, and how?"

Answer for the person's actual setup before installing anything. Find out:

| Question | If the answer is | Then |
|---|---|---|
| Which editor? | Premiere Pro | Read [Using Premiere Pro?](#using-premiere-pro) below. |
| | Another editor | The code does not apply. Only the pattern carries over: allowlist, plan → execute, journal, verify after write. |
| Which Resolve edition? | Free, not Studio | External scripting is unavailable. Stop here. |
| Which Resolve build? | 21.0.0.48 | Reads and writes work as tested. |
| | 21.1 or later | Resolve now has its own MCP server; read [Resolve 21.1 has its own MCP server](#resolve-211-has-its-own-mcp-server) first. This bridge stays read-only there until recalibrated. |
| | Anything else | Reads should work; writes are refused until [RECALIBRATION](docs/RECALIBRATION.md) passes on that build. Resolve 20 and earlier are untried. |
| Which OS? | macOS | Tested (arm64). |
| | Windows or Linux | Module paths have defaults, nothing was run. Treat as a port. |
| Python? | 3.10+, 64-bit, able to load Resolve's `fusionscript` | Fine. Tested on 3.13. |
| Which agent client? | Speaks stdio MCP | Use the snippets below. |

Then install, run `resolve-bridge doctor --json` with Resolve open, and report
to the person in plain terms: works as is, works read-only, or needs porting, and
what each `warn` or `fail` line means for them.

Ground rules while you set it up:

- Start with a disposable project and `allow_writes: false`.
- Do not add a build to `calibrated_builds`, clear `write_timeline_prefix`, or
  allowlist a real project on your own. Propose it and let the person decide.
- What was actually exercised live: timeline and media reads, clip append,
  markers, test-timeline creation, save and `.drp` backup, and the lower-third
  Fusion recipe. The MIDI piano roll has only run against fakes.
- Once it is running, [AGENTS.md](AGENTS.md) is your operating manual.

## Requirements

- DaVinci Resolve **Studio**. The free edition does not offer external scripting.
- Python 3.10 or newer, 64-bit, on the same machine as Resolve.
- An MCP client: Claude Code, Claude Desktop, Codex, or anything that speaks stdio MCP.

## Install

From a checkout:

```bash
git clone https://github.com/jts-nyc/nle-agent-bridge.git
```

```bash
cd nle-agent-bridge
```

```bash
python3 -m pip install .
```

That installs two commands: `resolve-bridge` (doctor, init, paths) and
`resolve-bridge-mcp` (the server). With [uv](https://docs.astral.sh/uv/) you can
skip the install and run straight from the checkout or a git URL:

```bash
uvx --from /path/to/nle-agent-bridge resolve-bridge doctor
```

```bash
uvx --from git+https://github.com/jts-nyc/nle-agent-bridge resolve-bridge-mcp
```

The package is not on PyPI.

## Enable scripting in Resolve

1. Open Resolve Studio and a project.
2. **Preferences › System › General › External scripting using → Local.**
3. Restart Resolve if the setting was just changed.

Then check everything in one go:

```bash
resolve-bridge doctor
```

`doctor` only reads. It reports the Python it is running in, the config it found,
whether a Resolve process exists, whether the scripting connection works, the
edition, whether the build is calibrated, and the open project's ID with the
exact line to paste into your allowlist. Exit code 0 means ready; 1 means a
`FAIL` line needs fixing. `--json` gives the same report for a script.

If the scripting module isn't found, point at it:

| Variable | Meaning | macOS default |
|---|---|---|
| `RESOLVE_SCRIPT_API` | Resolve's `Developer/Scripting` folder | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting` |
| `RESOLVE_SCRIPT_LIB` | The native `fusionscript` library | `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so` |

## Configure

```bash
resolve-bridge init
```

writes a starter config with an empty allowlist to the per-user config folder
(`resolve-bridge paths` prints every location in use). Edit it:

```json
{
  "allowed_projects": [
    {"id": "PROJECT-ID-FROM-DOCTOR", "name": "My test project", "allow_writes": false}
  ],
  "write_timeline_prefix": "AI_BRIDGE_TEST_",
  "midi_roots": [],
  "calibrated_builds": []
}
```

| Key | Default | Meaning |
|---|---|---|
| `allowed_projects` | `[]` | The only projects that can be read. `allow_writes` defaults to `false`. |
| `write_timeline_prefix` | `AI_BRIDGE_TEST_` | Timeline writes only on timelines whose name starts with this. `""` allows any timeline in a writable project. |
| `journal_path` | state folder | The SQLite write journal. Every caller must share one journal. |
| `backup_dir` | `project_backups/` beside the journal | Where `save_and_backup` exports `.drp` files. |
| `midi_roots` | `[]` | The only folders the MIDI tools may read. Empty refuses every path. |
| `calibrated_builds` | `[]` | Resolve builds you calibrated yourself; see [RECALIBRATION](docs/RECALIBRATION.md). |

Relative paths resolve against the config file's folder. Environment variables
override the file:

| Variable | Overrides |
|---|---|
| `RESOLVE_BRIDGE_CONFIG` | Which config file to load (same as `--config`) |
| `RESOLVE_BRIDGE_STATE_DIR` | The folder holding the journal, backups and reports |
| `RESOLVE_BRIDGE_JOURNAL` | `journal_path` |
| `RESOLVE_BRIDGE_BACKUP_DIR` | `backup_dir` |

Default locations: config in `$XDG_CONFIG_HOME/resolve-bridge/config.json`
(`~/.config/…`; `%APPDATA%` on Windows), state in
`$XDG_STATE_HOME/resolve-bridge/` (`~/.local/state/…`; `%LOCALAPPDATA%` on
Windows). With no config file the server still starts and refuses everything
except `resolve_status` and `convert_time`. A config named explicitly that
cannot be read is an error, not a fallback.

Start with a disposable project and `allow_writes: false`. Turn writes on for
that project only when you have seen the reads work.

## Connect your agent

Use the absolute path of the command (`which resolve-bridge-mcp`) wherever the
client does not inherit your shell's `PATH`. The server must run in a Python
that `resolve-bridge doctor` passes in.

### Claude Code

```bash
claude mcp add resolve --scope user -- resolve-bridge-mcp
```

With an explicit config or module path:

```bash
claude mcp add resolve --scope user -e RESOLVE_BRIDGE_CONFIG=/path/to/config.json -- resolve-bridge-mcp
```

Or in a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "resolve": {
      "command": "resolve-bridge-mcp",
      "args": [],
      "env": {"RESOLVE_BRIDGE_CONFIG": "/path/to/config.json"}
    }
  }
}
```

### Claude Desktop

Settings › Developer › Edit Config (`claude_desktop_config.json`). Desktop apps
do not see your shell `PATH`, so give the full path:

```json
{
  "mcpServers": {
    "resolve": {
      "command": "/full/path/to/resolve-bridge-mcp",
      "args": ["--config", "/full/path/to/config.json"]
    }
  }
}
```

Restart Claude Desktop afterwards.

### Codex

In `~/.codex/config.toml`:

```toml
[mcp_servers.resolve]
command = "/full/path/to/resolve-bridge-mcp"
args = ["--config", "/full/path/to/config.json"]
```

Codex's command sandbox blocked the connection to Resolve during development
when the bridge was run as a sandboxed shell command. Registered as an MCP
server it runs outside that sandbox; if `resolve_status` still reports
`resolve_unavailable` while `resolve-bridge doctor` passes in a terminal, the
launch environment is the difference.

### Any client with uvx

```json
{"command": "uvx", "args": ["--from", "git+https://github.com/jts-nyc/nle-agent-bridge", "resolve-bridge-mcp"]}
```

Then ask the agent to call `resolve_status`. Give the agent
[AGENTS.md](AGENTS.md): it is the operating manual for the tools.

## Safety model

What the server enforces, in the order a call meets it:

1. **Strict arguments.** Unknown fields, wrong types, out-of-range numbers and
   bad enums are rejected before Resolve is contacted.
2. **Allowlist plus open project.** Every project read and write names a
   `project_id`. It must be in your config and be the project open in Resolve.
   `resolve_status` shows the open project's name and ID even when it is not
   listed, and nothing inside it.
3. **Writes are opt-in, twice.** Per project (`allow_writes`) and per timeline
   name (`write_timeline_prefix`). The Fusion recipes keep their own
   `AI_BRIDGE_TEST_` rule even if you relax the prefix.
4. **Build pin.** On a Resolve build that has not been calibrated, every write
   tool refuses with `uncalibrated_build`. Reads still work.
5. **Plan, then execute.** `execute_*` tools take only a `plan_id` the server
   issued in this session, never an edited plan. The plan, the timeline
   snapshot and the media fingerprint are revalidated immediately before
   writing.
6. **Journal.** Intent is recorded in SQLite before Resolve is touched. One
   writer at a time. The same key and request replays the stored result; the
   same key with a different request is refused; a failed or interrupted attempt
   blocks automatic retry.
7. **Verification.** Each write is followed by a re-read and an exact check of
   what changed and that nothing else did.
8. **File reads are fenced.** MIDI files only from `midi_roots`, symlinks
   resolved first, `.mid`/`.midi` only, 2 MB cap, SHA-256 recorded in the plan.

What it does not do: delete, ripple, overwrite, relink, render, or touch source
media on disk. No such operation is implemented.

What it cannot protect against:

- A person editing in the Resolve GUI between the read and the write. Snapshot
  checks and read-after-write catch many such races, not all.
- Snapshots cover edit structure (tracks, clips, ranges, markers). Grades,
  effects, transitions, retimes and Fusion graphs are outside them.
- There is no rollback. A failed write may be partly applied; Undo groups are
  for you, in the GUI.
- `.drp` backups are checked for archive integrity. Restoring one is untested.
- If Resolve hangs mid-call, the tool call hangs until your client's MCP
  timeout. Afterwards a `running` journal record means the outcome is unknown.
- The server trusts its config. Anyone who can edit that file sets the policy,
  so don't give the agent write access to it.

## Resolve build pin

Two behaviours the writes rely on were established by live probes, not by
Blackmagic's documentation: `AppendToTimeline` treats the source end as
exclusive, and Fusion edits render only after the clip is loaded on the Fusion
page. Both were measured on **21.0.0.48**. On any other build the server is
read-only. `resolve_status` reports `calibrated_build` and `writes_available`.

To use writes on another build, follow [docs/RECALIBRATION.md](docs/RECALIBRATION.md):
repeat the probes in a disposable project, compare with the recorded results,
and only then add the build to `calibrated_builds`.

## Resolve 21.1 has its own MCP server

Resolve Studio 21.1 (September 2026) ships a built-in MCP server, enabled from
**File › Setup AI Assistants**. This bridge was written before it existed. The
details below come from third-party write-ups, not from Blackmagic's
documentation, and haven't been tested here:

- **Edition.** Studio only. 21.1 also makes external Python scripting
  Studio-only.
- **Tools.** One guide counts 14 tools in 21.1.2. Most of the work goes through
  two of them: `run_script`, which runs Python in a sandbox that reportedly
  blocks file, network and process access, and `run_script_unsafe`, which
  Blackmagic describes as having full system access. The rest cover the API
  documentation, LUTs and DCTLs, and launching Resolve.
- **API.** 21.1 adds about twenty scripting calls, including transitions,
  multicam, audio normalisation, render presets and transcripts. Reviewers
  report it still can't freely move, trim or split clips, and grade values have
  no calls.

How it compares:

| | Built-in server | This bridge |
|---|---|---|
| Reach | Anything the scripting API can do, by the agent writing scripts | A fixed set of 25 tools |
| Project scoping | None described | Allowlist plus the open-project check |
| Before a write | The agent's own script | A plan the server validated, then execute |
| Record and retry protection | None described | Journal, idempotency keys, no automatic retry |
| After a write | None described | Re-read and exact verification |
| Build differences | Not addressed | Writes pinned to calibrated builds |

Which to use: for broad access with a person watching every step, the built-in
server is less setup. For unattended or repeated edits, or anywhere a wrong
write is expensive, this bridge's guardrails are the point.

You can run both, but they don't coordinate. Writes made through the built-in
server bypass this bridge's allowlist, journal and lock, so a timeline both
touch can change under a plan. If you use both, give the built-in server
`run_script` only, keep `run_script_unsafe` turned off or set to ask every
time, and re-read before planning.

On 21.1 this bridge is read-only until [RECALIBRATION](docs/RECALIBRATION.md)
passes. It doesn't use any of the new 21.1 calls yet.

## Using Premiere Pro?

This bridge drives Resolve only, and there is no Premiere version. What follows
is from research in November 2026, not from running anything in Premiere. Check
each point against the Premiere version in front of you.

**Adobe's own Claude connector does not do this.** Its video tools render,
resize and quick-cut files uploaded to Adobe's cloud. None of them opens a
Premiere project or edits its sequences, bins or markers.

**How Premiere is scripted:**

- **UXP** is the current official API, released in Premiere 25.6. The
  `premierepro` module covers projects, sequences, tracks, clips, markers and
  project items. Clip edits are `SequenceEditor` actions such as
  `createInsertProjectItemAction`. Every `create*Action()` call has to happen
  inside `project.lockedAccess()` and be applied inside
  `project.executeTransaction()`, and both callbacks must be synchronous.
  Adobe's `@adobe/eslint-plugin-premierepro` checks for this.
- **ExtendScript and CEP** still work. In August 2026 Adobe staff said no
  end-of-life date is set for Premiere. Some community posts mention September
  2026; that date did not come from an Adobe announcement.
- **Gaps:** Time Remapping is not reachable from either API as of 26.3, and
  MOGRT parameter control was still in progress.

**Two differences from Resolve that change the design:**

1. **The code has to run inside Premiere.** Resolve accepts a connection from an
   outside Python process. Premiere does not. A UXP plugin can open a WebSocket
   to another process but cannot accept incoming connections. A port would
   therefore keep the Python MCP server, have it host a localhost WebSocket, and
   add a small UXP plugin that connects to it and runs a fixed set of commands.
   Reports on localhost entries in the plugin manifest's
   `requiredPermissions.network` are inconsistent, so test that first.
2. **Premiere can report success for an edit it did not make.** Reported on
   26.3.2: In/Out setters return success while the values stay unchanged, and
   `importFiles` returns `true` for a path that does not exist. Treat every
   return value as unconfirmed until a re-read shows the change.

**What a port can reuse from this repository:**

| Reuse as is | Adapt | Rewrite as a UXP plugin (JavaScript) |
|---|---|---|
| `mcp_server.py` protocol and argument validation, `config.py`, `journal.py`, `diff.py`, `errors.py` | `timecode.py` (Premiere counts time in ticks, 254,016,000,000 per second), `snapshot.py` and `models.py`, `calibration.py` (pin Premiere builds instead) | `connection.py`, `timeline.py`, `media.py`, `operations.py`, `project.py`, `fusion.py`, `midi_roll.py` |

A first version would cover reads, append, markers and a project save. It needs
a Premiere install to calibrate against, the same way the Resolve writes were
calibrated on 21.0.0.48.

**Existing community Premiere MCP servers.** None is from Adobe, and none was
tested here. They offer far more tools than this bridge, and less of its safety
model:

| Project | How it reaches Premiere | Notes from its own docs |
|---|---|---|
| [hetpatel-11/Adobe_Premiere_Pro_MCP](https://github.com/hetpatel-11/Adobe_Premiere_Pro_MCP) | ExtendScript run by a CEP panel, passed through files in `/tmp` | MIT. Tested on 26.0, macOS. About 280 tools, including `execute_extendscript`, which runs any script. Has undo and some read-back. No allowlist, dry run or journal. |
| [ayushozha/AdobePremiereProMCP](https://github.com/ayushozha/AdobePremiereProMCP) | Not checked | Claims over 1,000 tools. |
| [mikechambers/adb-mcp](https://github.com/mikechambers/adb-mcp) | Plugins across several Adobe apps | Proof of concept; says its Premiere side is limited. |
| [antipaster/Adobe-Premiere-Pro-MCP](https://github.com/antipaster/Adobe-Premiere-Pro-MCP) | CEP panel | Listed requirements include Windows. |

If you use one of these, check before turning it loose on a real project:

- Does it have a tool that runs arbitrary script? Can that tool be switched off?
- Can it be limited to one project?
- Does it re-read state after each write?
- What happens when a write fails halfway?

Sources: [Premiere UXP API](https://developer.adobe.com/premiere-pro/uxp),
[UXP changelog](https://developer.adobe.com/premiere-pro/uxp/changelog),
[SequenceEditor](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/sequenceeditor),
[network recipe](https://developer.adobe.com/premiere-pro/uxp/resources/recipes/network),
[ESLint plugin](https://blog.developer.adobe.com/en/publish/2026/08/new-eslint-plugin-catches-common-premiere-uxp-bugs),
[ExtendScript support status](https://forums.creativeclouddeveloper.com/t/uxp-api-parity-for-mogrt-parameter-control-current-extendscript-support-status/12131),
[success-before-applied report](https://forums.creativeclouddeveloper.com/t/uxp-premiere-a-class-of-apis-report-success-resolve-before-the-operation-is-applied-or-validated-in-out-setters-importfiles-importaecomps-create-action-repro-in-shipping-26-3-2/12358),
[Time Remapping gap](https://forums.creativeclouddeveloper.com/t/time-remapping-component-is-completely-inaccessible-via-scripting-extendscript-and-uxp-premierepro-api-in-premiere-pro-26-3-0/12026).

## More

- [AGENTS.md](AGENTS.md): tool catalog, call order and never-do list for the agent.
- [docs/MCP.md](docs/MCP.md): server internals and a live smoke test.
- [docs/REFERENCE.md](docs/REFERENCE.md): editing contract, snapshots, timecode rules, library layout.
- [docs/FUSION.md](docs/FUSION.md): the Fusion recipes and their limits.
- [docs/RECALIBRATION.md](docs/RECALIBRATION.md): the build pin runbook.

## License

[MIT](LICENSE).

## Development

```bash
python3 -m pip install -e '.[test]'
```

```bash
python3 -m pytest -q
```

Unit tests never connect to Resolve. Read-only live tests are opt-in:

```bash
RESOLVE_BRIDGE_LIVE=1 RESOLVE_BRIDGE_PROJECT_ID=PROJECT_ID python3 -m pytest tests/integration -q
```

Two MIDI cross-check tests compare against other parsers and skip unless
`AE_SCRIPTS_DIR` and `MIDI_HERO_DIR` point at those checkouts.

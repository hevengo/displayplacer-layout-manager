# DisplayPlacer Layout Manager

Automatically detect connected monitors, identify them by hardware serial, and apply predefined display layouts on macOS.

Built on top of [displayplacer](https://github.com/jakehilborn/displayplacer).

## Prerequisites

This tool is **macOS-only** — it relies on CoreDisplay, CoreGraphics, and IOKit private APIs available only on macOS.

### displayplacer

[displayplacer](https://github.com/jakehilborn/displayplacer) is the underlying command-line tool that reads and applies display arrangements.

```bash
brew install displayplacer
```

The script checks for `displayplacer` at runtime and exits with an install hint if it is not found.

### uv

[uv](https://docs.astral.sh/uv/) is a fast Python package manager and script runner. The script uses uv's inline script metadata (`uv run --script`) for zero-dependency execution — no virtualenv or `pip install` needed.

Install via Homebrew:

```bash
brew install uv
```

Or via the standalone installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Python

Python 3.11+ is required. If you installed uv, it will download and manage a suitable Python version automatically — no manual Python install needed.

## Usage

```bash
./display-layout-manager.py
```

Or explicitly via uv:

```bash
uv run --script display-layout-manager.py
```

With no arguments, the script shows an ASCII diagram of the current display arrangement and prints detailed info for each connected monitor (active and disabled).

Use subcommands for specific actions:

- `./display-layout-manager.py auto` — auto-apply the preferred layout for the current display set
- `./display-layout-manager.py switch` — list all layouts and interactively select one to apply
- `./display-layout-manager.py config` — interactive display and layout configuration editor
- `./display-layout-manager.py reset` — re-enable all disabled displays
- `./display-layout-manager.py init` — detect connected displays and generate a new `config.yml`
- `./display-layout-manager.py daemon` — run as a persistent daemon (see [Daemon mode](#daemon-mode) below)

Run `./display-layout-manager.py -h` to see all available subcommands.

### Example output

```
  (B) [4] ASUS PG32UQ — 32" main (main)
      Resolution: 2560x1440 @ 120Hz  Color depth: 8  Scaling: on
      Origin: (0,0)  Rotation: 0°  Enabled: true
      Persistent: AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE
      Contextual: 4
      match:
        serial: s987654
        brand: ASUS
        production_year: 2022
        production_week: 3
        product_name: ASUS PG32UQ

  (C1) [2] ASUS VG28UQL1A — 28" left
      Resolution: 2304x1296 @ 100Hz  Color depth: 8  Scaling: on
      ...
      match:
        serial: s111222333
        edid_serial: ABCDEF012345
        brand: ASUS
        production_year: 2021
        production_week: 4
        product_name: VG28UQL1A
```

The `match:` block is formatted as valid YAML — copy the fields you need directly into your `config.yml`.

## Configuration

All displays and layouts are defined in `config.yml` (located next to the script).

### Option A: Auto-generate from connected displays

```bash
./display-layout-manager.py init
```

Detects all connected monitors and writes a `config.yml` with pre-filled match fields, current display settings, and a starter layout. If `config.yml` already exists, a timestamped backup is created first.

### Option B: Start from the example template

```bash
cp config.example.yml config.yml
```

Then run `./display-layout-manager.py config` for an interactive editor, or edit the file directly.

## Adding a new monitor

### Step 1: Find the monitor's match fields

Run the script with no arguments to see connected displays. Each display prints a `match:` block with all available identification fields:

```
      match:
        serial: s987654
        brand: ASUS
        production_year: 2022
        production_week: 3
        product_name: ASUS PG32UQ
```

Only `serial` is required. The other fields are optional filters — add them when you need to distinguish monitors that share the same serial (e.g. identical models, or different brands with a colliding serial).

### Step 2: Add a display entry

Add an entry to the `displays` list in `config.yml`. Copy the `match` fields from the script output and add `settings` as needed:

```yaml
- id: office-27
  label: Dell U2723QE 27"
  match:
    serial: s123456
    brand: Dell
    product_name: DELL U2723QE
  settings:
    resolution: 2560x1440
    hertz: 60
    color_depth: 8
    scaling: true
    enabled: true
```

**Display fields:**

| Field | Required | Description |
|---|---|---|
| `id` | yes | Your chosen name, used to reference this display in layouts |
| `label` | yes | Human-readable name shown in output (e.g. `Dell U2723QE 27"`) |
| `match.serial` | yes | The `Serial screen id` from `displayplacer list` (e.g. `s987654`) |
| `match.edid_serial` | no | EDID alphanumeric serial — only needed to distinguish identical models |
| `match.brand` | no | Manufacturer name (e.g. `ASUS`, `Dell`) or PNP code (e.g. `AUS`). Case-insensitive |
| `match.production_year` | no | Year of manufacture from EDID (e.g. `2021`) |
| `match.production_week` | no | Week of manufacture from EDID (1–53) |
| `match.product_name` | no | Model name from EDID (e.g. `VG28UQL1A`). Case-insensitive |
| `settings.resolution` | no | Desired resolution (e.g. `2560x1440`). Omit to keep current |
| `settings.hertz` | no | Desired refresh rate. Omit to keep current |
| `settings.fallback_hertz` | no | Alternative refresh rate if the primary isn't available |
| `settings.color_depth` | no | Color depth (typically `8`). Omit to keep current |
| `settings.scaling` | no | `true` or `false`. Omit to keep current |
| `settings.enabled` | no | `true` or `false` (default: `true`). Set `false` to disable this display |

### Handling identical monitors

When two monitors share the same numeric serial (common with same-model pairs), set `edid_serial` on each to distinguish them:

```yaml
- id: left-28
  label: ASUS VG28UQL1A 28" (left)
  match:
    serial: s111222333
    edid_serial: ABCDEF012345
  settings:
    resolution: 2304x1296
    hertz: 100
    fallback_hertz: 60
    color_depth: 8
    scaling: true

- id: right-28
  label: ASUS VG28UQL1A 28" (right)
  match:
    serial: s111222333
    edid_serial: '9876543210123'      # quoted — purely numeric string
  settings:
    resolution: 2304x1296
    hertz: 100
    fallback_hertz: 60
    color_depth: 8
    scaling: true
```

The EDID serial is read via a CoreDisplay + ioreg identification chain (see `display-identification-facts.md` for the full technical investigation). If the chain fails at runtime, the script falls back to contextual-ID ordering and offers both layout permutations.

## Adding a layout

Layouts define left-to-right monitor positioning for a given device set. Add entries to the `layouts` list in `config.yml`.

```yaml
- name: Center + Office
  match: [center-32, office-27]
  positions: [office-27, center-32]
  main: center-32
  preferred: true
```

**Layout fields:**

| Field | Required | Description |
|---|---|---|
| `name` | yes | Display name shown in output and prompts |
| `positions` | yes | Display ids in left-to-right physical order |
| `main` | yes | Which display sits at origin (0,0); others are placed relative to it |
| `match` | no | Sorted set of display IDs. The layout auto-applies when the connected displays match this set exactly. If omitted, the layout is only available via `switch` |
| `preferred` | no | `true` to auto-apply when multiple layouts share the same match set (at most one per match set) |
| `enabled` | no | Which displays should be active. Defaults to `positions` if omitted |
| `disabled` | no | Display ids to disable for this layout |

The script calculates pixel origins automatically. Screens are placed edge-to-edge left-to-right, vertically centered relative to the main display.

### Multiple layouts per match set

If multiple layouts share the same `match` set and none is marked `preferred`, the script presents an interactive picker instead of auto-applying.

## How monitor identification works

The script uses several layers of identification, checked in order of specificity:

1. **EDID alphanumeric serial** (`edid_serial`) — read from the EDID extension block via the IOKit registry. Unique per physical unit. Requires the CoreDisplay identification chain to map contextual IDs to IOKit framebuffers. This is the most reliable match when available.

2. **Numeric serial** (`serial`) — from `displayplacer list`. Unique for most monitor models, but identical-model pairs often share the same value. Always required as the baseline match key.

3. **Hardware metadata** (`brand`, `production_year`, `production_week`, `product_name`) — optional filters read from EDID via ioreg. Useful for narrowing matches when multiple displays share the same serial but differ in brand, model, or production date.

4. **Contextual ID fallback** — when hardware identification fails, monitors with duplicate serials are assigned to known screens by sorting on their macOS contextual display ID. This is deterministic within a session but may differ across reboots.

The identification chain:

```
CGDisplayID (contextual)
  → CoreDisplay_DisplayCreateInfoDictionary()  [private API, ctypes]
    → IODisplayLocation path → dispext index
      → ioreg AppleCLCD2 DisplayAttributes      [plistlib]
        → AlphanumericSerialNumber
```

See `display-identification-facts.md` for the full technical investigation and caveats.

## Daemon mode

The script can run as a persistent daemon that automatically re-applies the display layout when:

- The system **wakes from sleep** (layout at 5s, 10s, 15s after wake)
- A display is **added or removed** — clamshell open/close, hot-plug (layout at 2s, 5s, 10s)
- On **startup** (covers the login case)

```bash
./display-layout-manager.py daemon
```

Multiple events in quick succession are debounced — pending timers are cancelled and rescheduled. The daemon uses IOKit power notifications and CoreGraphics display reconfiguration callbacks via `ctypes` — no additional dependencies required.

### Menu bar integration

The daemon can optionally show a macOS menu bar icon for quick layout switching. Enable it in `config.yml`:

```yaml
options:
  enable-menu-bar: true
```

When enabled, the daemon displays a monitor icon in the menu bar with:

- A dropdown listing all defined layouts (checkmark on the active one)
- A **Reset displays** item to re-enable all disabled displays
- macOS notifications on layout apply success or failure
- Global hotkey **Ctrl+Option+Cmd+R** to reset displays from anywhere

The menu bar uses the `rumps` Python package, which is listed in the script's inline dependencies and installed automatically by `uv`.

### Installing as a LaunchAgent

To have the daemon start automatically on login and restart if it crashes:

```bash
./display-layout-manager.py install
```

This creates a LaunchAgent plist at `~/Library/LaunchAgents/com.user.displayplacer.plist`, loads it via `launchctl`, and starts the daemon immediately.

Log output goes to `/tmp/displayplacer-daemon.log`.

### Checking status

```bash
./display-layout-manager.py status
```

Shows whether the plist is installed, the daemon PID if running, and the log path.

### Starting the daemon

```bash
./display-layout-manager.py start
```

Starts the daemon via the installed LaunchAgent.

### Stopping the daemon

```bash
./display-layout-manager.py stop
```

Stops the daemon but keeps the plist installed — it will start again on next login.

### Restarting the daemon

```bash
./display-layout-manager.py restart
```

Force-restarts the daemon. Useful after editing `config.yml` while the daemon is running.

### Removing the LaunchAgent

```bash
./display-layout-manager.py uninstall
```

Stops the daemon and removes the plist entirely.

### Viewing the log

```bash
tail -f /tmp/displayplacer-daemon.log
```

## Command reference

| Command | Description |
|---|---|
| *(no command)* | Show ASCII diagram of current display arrangement with detailed info |
| `init` | Detect connected displays and generate a new `config.yml` |
| `config` | Interactive display and layout configuration editor |
| `auto` | Auto-apply the preferred layout for the current display set |
| `switch` | List all layouts and interactively select one to apply |
| `reset` | Re-enable all disabled displays |
| `daemon` | Run as a persistent daemon, re-applying layout on wake and display changes |
| `install` | Install the LaunchAgent plist and start the daemon |
| `uninstall` | Stop the daemon and remove the LaunchAgent plist |
| `start` | Start the daemon via the installed LaunchAgent |
| `stop` | Stop the daemon (keeps plist installed; restarts on next login) |
| `restart` | Force restart the daemon via the installed LaunchAgent |
| `status` | Show whether the LaunchAgent is installed and running |

## Troubleshooting

### displayplacer not found

Install via Homebrew: `brew install displayplacer`. The script checks for it at runtime and exits with an install hint if missing.

### Layout not applying on wake

The daemon retries layout application at 5s, 10s, and 15s after wake to account for macOS display initialization timing. If the layout still doesn't apply, check the log for errors:

```bash
tail -f /tmp/displayplacer-daemon.log
```

### Identical monitors are swapped

When two monitors share the same numeric serial (common with same-model pairs), set `edid_serial` on each display entry to distinguish them. Run the script with no arguments to see each display's `match:` block including the EDID serial when available.

### Menu bar icon not showing

Ensure `enable-menu-bar: true` is set in the `options:` section of `config.yml`. The menu bar requires the daemon to be running (`daemon`, `install`, or `start` command).

### Layout takes longer with disabled displays

When a layout disables displays, the script uses a three-phase apply sequence: re-enable needed displays, reposition all displays, then disable unwanted ones. Each phase includes stabilization waits for macOS to finish reconfiguring. This is expected behavior.

---
name: Menu bar integration
overview: Add an optional macOS menu bar icon to the daemon using rumps, gated by a new `options.enable-menu-bar` config flag. All layouts are listed in the menu; clicking one applies it on a background thread. A "Reset displays" entry re-enables all disabled displays.
todos:
  - id: options-dataclass
    content: Add `Options` dataclass and parse `options:` in `load_config()`; update all 5 call sites for the new 3-tuple return value
    status: completed
  - id: config-files
    content: Add `options:` section to `config.yml` and `config.example.yml` (before `displays:`)
    status: completed
  - id: rumps-dep
    content: Add `rumps` to inline script dependencies on line 4
    status: completed
  - id: menubar-app
    content: Implement `LayoutMenuBarApp(rumps.App)` class with menu population, layout-click callback, reset callback, and non-blocking threading
    status: completed
  - id: daemon-integration
    content: Refactor `daemon_main` to conditionally use rumps app loop or bare CFRunLoop based on `options.enable_menu_bar`
    status: completed
  - id: quality-gates
    content: "Run all quality gates: syntax check, headless daemon, menu bar daemon, layout click, reset click, wake/reconfig callbacks"
    status: completed
isProject: false
---

# Menu Bar Integration via rumps

## Project context

This is a **single-file Python script** ([display-layout-manager.py](display-layout-manager.py), ~2766 lines) that manages macOS display arrangements using the `displayplacer` CLI tool. It runs via `uv run --script` with inline metadata for dependencies. Config lives in [config.yml](config.yml). There is no test suite.

The daemon mode (`daemon_main`, line 1549) registers IOKit power notifications and CoreGraphics display-change callbacks, then enters `CFRunLoopRun()`. The key architectural insight for this feature: `**NSApplication.run()` (used by rumps) drives the main thread's `CFRunLoop`**, so all existing IOKit/CG callbacks will continue to fire when we swap `CFRunLoopRun()` for `rumps.App.run()`.

---

## 1. Options dataclass and load_config changes

### 1a. New dataclass

Add after the `Layout` dataclass (after line 95):

```python
@dataclass
class Options:
    enable_menu_bar: bool = False
```

### 1b. Parse options in load_config

Current signature and return (line 151-153, 316):

```
151:316:display-layout-manager.py
def load_config(
    path: Path,
) -> tuple[dict[str, KnownScreen], dict[tuple[str, ...], list[Layout]]]:
    # ...
    return known_screens, device_set_layouts
```

Change to:

```python
def load_config(
    path: Path,
) -> tuple[dict[str, KnownScreen], dict[tuple[str, ...], list[Layout]], Options]:
```

Add options parsing right after `raw = ry.load(f)` and the `isinstance` check (after line 163), before the displays section:

```python
    # --- options ---
    raw_options = raw.get("options") or {}
    options = Options(
        enable_menu_bar=bool(raw_options.get("enable-menu-bar", False)),
    )
```

Change the return on line 316 to:

```python
    return known_screens, device_set_layouts, options
```

### 1c. Update all 5 call sites

Every place that calls `load_config()` must destructure the third element. Here are all of them:

- **Line 2089** (inside `setup_main` / config editor):
`known_screens, device_set_layouts = load_config(config_path)` -> add `, _`
- **Line 2614** (inside `switch_main`):
`known_screens, device_set_layouts = load_config(config_path)` -> add `, _`
- **Line 2665** (inside `auto_main`):
`known_screens, device_set_layouts = load_config(config_path)` -> add `, _`
- **Line 2757** (daemon case in `main()`):
`known_screens, device_set_layouts = load_config(_CONFIG_PATH)` -> change to `known_screens, device_set_layouts, options = load_config(_CONFIG_PATH)` and pass `options` to `daemon_main`
- **Line 2760** (default case in `main()`):
`known_screens, _ = load_config(_CONFIG_PATH)` -> change to `known_screens, *_ = load_config(_CONFIG_PATH)`

---

## 2. Config files

### 2a. config.yml

Insert before line 35 (`displays:`), after the header comments:

```yaml
# -----------------------------------------------------------------------------
# options: global settings
# -----------------------------------------------------------------------------

options:
  enable-menu-bar: false


```

### 2b. config.example.yml

Same placement (before line 39 `displays:`), with a documenting comment:

```yaml
# -----------------------------------------------------------------------------
# options: global settings
#
#   enable-menu-bar - when true, the daemon shows a macOS menu bar icon
#                     that lists all layouts for quick switching (default: false)
# -----------------------------------------------------------------------------

options:
  enable-menu-bar: false


```

---

## 3. Add rumps dependency

Line 4 of `display-layout-manager.py`:

```
4:4:display-layout-manager.py
# dependencies = ["ruamel.yaml"]
```

Change to:

```python
# dependencies = ["ruamel.yaml", "rumps"]
```

`rumps` transitively pulls in `pyobjc-framework-Cocoa` and `pyobjc-core`. These are cached by `uv` after first install.

**Important**: `import rumps` must only happen inside the menu-bar code path (conditional import inside `daemon_main`), not at module level. This keeps the headless daemon and all other subcommands free of the PyObjC import cost.

---

## 4. LayoutMenuBarApp class

Place this **inside `daemon_main`** (or as a nested class/function within it) so it closes over `known_screens`, `device_set_layouts`, and the ctypes references. Alternatively, place it at module level and pass state via constructor -- either works.

### Menu bar title

Use the monochrome desktop computer glyph (no external icon file):

```python
title = "\U0001F5A5\uFE0E"   # U+1F5A5 DESKTOP COMPUTER + U+FE0E text presentation selector
```

The `FE0E` variation selector forces macOS to render it as a monochrome text glyph rather than a color emoji.

### Menu structure

```
[monitor-glyph]
 +-- Full desk: left | center | right | macbook    [checkmark if last-applied]
 +-- External only (macbook off)
 +-- Right-Only
 +-- Center-Only
 +-- ────────────
 +-- Reset displays
 +-- ────────────
 +-- Quit
```

- **All layouts** from config are listed (iterate all values in `device_set_layouts`), not filtered by connected displays.
- A checkmark (`MenuItem.state = 1`) marks the last-applied layout.
- "Reset displays" re-enables all disabled displays (same logic as `reset_main()`, line 2585).
- "Quit" uses the built-in `rumps` quit button.

### Skeleton

```python
import rumps

class LayoutMenuBarApp(rumps.App):
    def __init__(self, known_screens, device_set_layouts):
        super().__init__(
            "DisplayPlacer",
            title="\U0001F5A5\uFE0E",
            quit_button="Quit",
        )
        self._known_screens = known_screens
        self._device_set_layouts = device_set_layouts
        self._current_layout_name: str | None = None
        self._busy = False
        self._build_menu()

    def _all_layouts(self) -> list[Layout]:
        return [lay for group in self._device_set_layouts.values() for lay in group]

    def _build_menu(self):
        self.menu.clear()
        for lay in self._all_layouts():
            item = rumps.MenuItem(lay.name, callback=self._on_layout_click)
            if lay.name == self._current_layout_name:
                item.state = 1
            self.menu.add(item)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Reset displays", callback=self._on_reset_click))

    def _on_layout_click(self, sender):
        if self._busy:
            return
        layout = next((l for l in self._all_layouts() if l.name == sender.title), None)
        if not layout:
            return
        self._busy = True
        t = threading.Thread(target=self._do_apply, args=(layout,), daemon=True)
        t.start()

    def _do_apply(self, layout):
        try:
            displays = parse_displays(run_displayplacer_list())
            displays.extend(_disabled_display_objects())
            hw_map = build_hw_info_map()
            matched, _ = match_displays(displays, self._known_screens, hw_map)
            if matched:
                _apply_layout(layout, matched, self._known_screens)
                self._current_layout_name = layout.name
                _log(f"Menu: applied {layout.name}")
            else:
                _log("Menu: no matched displays")
        except Exception as exc:
            _log(f"Menu: error applying layout: {exc}")
        finally:
            self._busy = False
            rumps.Timer(0, lambda _: self._build_menu()).start()

    def _on_reset_click(self, sender):
        if self._busy:
            return
        self._busy = True
        t = threading.Thread(target=self._do_reset, daemon=True)
        t.start()

    def _do_reset(self):
        try:
            disabled = _get_disabled_displays()
            if disabled:
                ids = [d for d, *_ in disabled]
                _log(f"Menu: re-enabling {len(ids)} display(s)")
                _reenable_displays(ids)
            else:
                _log("Menu: all displays already active")
        except Exception as exc:
            _log(f"Menu: reset error: {exc}")
        finally:
            self._busy = False
            rumps.Timer(0, lambda _: self._build_menu()).start()
```

### Non-blocking pattern

`_apply_layout` (line 1326) can block for up to 10 seconds when it needs to re-enable displays (the `time.sleep()` loop at lines 1346-1361). Therefore:

- **Every** layout click and reset click dispatches to a `threading.Thread(daemon=True)`.
- While busy, `self._busy = True` causes additional clicks to be ignored.
- When done, a zero-delay `rumps.Timer` callback fires on the main thread to rebuild the menu (update checkmarks, re-enable items). `rumps.Timer` callbacks run on the main thread, making them safe for AppKit UI updates.

The existing `safe_apply()` inside `daemon_main` (used by IOKit/CG callbacks) already runs on `threading.Timer`. When menu bar is active, extend `safe_apply` to also trigger a menu rebuild on completion via the same `rumps.Timer(0, ...)` pattern.

---

## 5. Refactor daemon_main

### Signature change

```
1549:1552:display-layout-manager.py
def daemon_main(
    known_screens: dict[str, KnownScreen],
    device_set_layouts: dict[tuple[str, ...], list[Layout]],
) -> int:
```

Add `options: Options` parameter:

```python
def daemon_main(
    known_screens: dict[str, KnownScreen],
    device_set_layouts: dict[tuple[str, ...], list[Layout]],
    options: Options,
) -> int:
```

### Branching logic

The function body from line 1553-1681 stays mostly the same. The structural change is at the end. Currently the function ends with:

```
1678:1681:display-layout-manager.py
    _log("Listening for wake and display events")
    cf.CFRunLoopRun()
    _log("Daemon stopped")
    return 0
```

When `options.enable_menu_bar` is `True`:

1. **Conditional import**: `import rumps` at the top of the function body (not module level).
2. **Create app**: Instantiate `LayoutMenuBarApp`.
3. **Extend `safe_apply`**: After `apply_current_layout(...)`, also call `rumps.Timer(0, lambda _: app._build_menu()).start()` so the menu reflects auto-applied layouts.
4. **Replace the run loop**: Instead of `cf.CFRunLoopRun()`, call `app.run()`.
5. **Shutdown handler**: Replace `cf.CFRunLoopStop(run_loop)` with `rumps.quit_application()` in the `_shutdown` signal handler.

When `options.enable_menu_bar` is `False`: the function runs exactly as today. No rumps import, no menu bar, just `CFRunLoopRun()`.

### IOKit / CG callback registration

The IOKit power source registration (lines 1655-1664) and CG display reconfig callback (line 1666) happen **before** the run loop starts. This is fine for both paths -- `CFRunLoopAddSource` adds to the main thread's run loop regardless of whether it will later be driven by `CFRunLoopRun()` or `NSApplication.run()`. No changes needed to the callback registration code.

### Signal handling

Current shutdown handler (line 1669-1673):

```
1669:1676:display-layout-manager.py
    def _shutdown(signum, _frame):
        _log(f"Signal {signum} received, shutting down")
        cancel_pending()
        cg.CGDisplayRemoveReconfigurationCallback(_reconfig_cb, None)
        cf.CFRunLoopStop(run_loop)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
```

When menu bar is active, replace `cf.CFRunLoopStop(run_loop)` with `rumps.quit_application()`. Both achieve the same thing (stop the main event loop), but `rumps.quit_application()` properly tears down the `NSApplication`.

---

## 6. CLI wiring

In `main()` (line 2705), the daemon case on line 2756-2758:

```
2756:2758:display-layout-manager.py
        case "daemon":
            known_screens, device_set_layouts = load_config(_CONFIG_PATH)
            return daemon_main(known_screens, device_set_layouts)
```

Changes to:

```python
        case "daemon":
            known_screens, device_set_layouts, options = load_config(_CONFIG_PATH)
            return daemon_main(known_screens, device_set_layouts, options)
```

---

## Files changed

- [display-layout-manager.py](display-layout-manager.py) -- Options dataclass, load_config changes, all call site updates, rumps dependency, LayoutMenuBarApp class, daemon_main refactor
- [config.yml](config.yml) -- add `options:` section before `displays:`
- [config.example.yml](config.example.yml) -- add `options:` section with documentation comment

No new files.

---

## Quality gates

Run these checks in order before considering the task done.

### Gate 1: Syntax check

```bash
python3 -c "import py_compile; py_compile.compile('display-layout-manager.py', doraise=True)"
```

Must exit 0.

### Gate 2: Config loads with new options section

```bash
uv run --script display-layout-manager.py
```

With `enable-menu-bar: false` in config.yml, the default subcommand (show displays) must work exactly as before. This validates that `load_config` still parses correctly and all call sites handle the new return value.

### Gate 3: Headless daemon still works

```bash
uv run --script display-layout-manager.py daemon
```

With `enable-menu-bar: false`. Must start, log "Listening for wake and display events", and respond to Ctrl+C cleanly. Confirms the headless path is unbroken.

### Gate 4: Menu bar daemon starts

```bash
# Edit config.yml: set enable-menu-bar: true
uv run --script display-layout-manager.py daemon
```

Must show a monochrome monitor glyph in the macOS menu bar. Clicking it must show all layouts, "Reset displays", and "Quit". Ctrl+C or "Quit" must exit cleanly.

### Gate 5: Layout click applies non-blockingly

Click a layout in the menu. Verify:

- The layout is applied (displays rearrange)
- The menu bar stays responsive during application (not frozen)
- A checkmark appears next to the applied layout after completion
- The daemon log shows `Menu: applied <name>`

### Gate 6: Reset click works

Click "Reset displays". Verify:

- Disabled displays are re-enabled (or "all displays already active" is logged)
- The menu bar stays responsive

### Gate 7: Wake/reconfig callbacks still fire

With menu bar active, sleep and wake the Mac (or plug/unplug a display). Verify the daemon log shows the usual "Wake detected" or "Display reconfiguration detected" messages and the layout is re-applied automatically.
# Display Identification on Apple Silicon Macs

## Problem

When two identical monitors (ASUS VG28UQL1A, 28") are connected to an Apple Silicon MacBook Pro, macOS assigns them the same numeric EDID serial (`111222333` / `0x069F6E7D` — a dummy value). This makes it impossible to distinguish which physical monitor is which using the standard IDs exposed by `displayplacer`, `system_profiler`, or `CoreGraphics`.

The goal is to reliably map each physical monitor to its macOS contextual display ID so that display arrangements can be applied automatically without user trial-and-error.

## Hardware Setup

| Label | Model | Serial ID (displayplacer) | Notes |
|-------|-------|---------------------------|-------|
| A | MacBook Pro built-in | `s1234567890` | Unique serial, trivially identified |
| B | ASUS PG32UQ 32" | `s987654` | Unique serial, trivially identified |
| C1 | ASUS VG28UQL1A 28" | `s111222333` | Shared serial with C2 |
| C2 | ASUS VG28UQL1A 28" | `s111222333` | Shared serial with C1 |

## Data Sources Investigated

### 1. displayplacer

Provides three IDs per display:

- **Persistent UUID** — different for each C screen but **not stable across wakeups**. Confirmed by examining the WindowServer plist (`com.apple.windowserver.displays.*.plist`) which stores many different historical UUIDs for the same physical screens.
- **Contextual ID** — stable within a session, maps 1:1 to `CGDisplayID`. Changes across reboots/wakeups.
- **Serial ID** — the numeric EDID serial. Identical for both C screens (`s111222333`).

### 2. system_profiler SPDisplaysDataType -json

Returns `_spdisplays_displayID` (= contextual ID) but all other fields are identical for both C screens:

- Same `_spdisplays_display-serial-number`: `"69F6E7D"`
- Same `_spdisplays_display-week`: `"4"`
- Same `_spdisplays_display-year`: `"2021"`
- Same `_spdisplays_display-product-id`: `"28a0"`
- Same `_name`: `"ASUS Monitor"`

The "match by manufacture week/year" approach (used in [FloWi's gist](https://gist.githubusercontent.com/FloWi/107ba7e80ea4411e8935da7cbc38df0e/raw/29e781eff10c584a304391e1d827c972966b9b2e/get-correct-display-setup.py)) fails because both monitors were produced in the same week.

### 3. ioreg (IOKit Registry)

The `AppleCLCD2` framebuffer entries under `dispext0`, `dispext1`, etc. each have a `DisplayAttributes` dict containing:

- **`PortID`** — unique per framebuffer (e.g., `32`, `209768448`)
- **`AlphanumericSerialNumber`** — the true unique hardware serial from the EDID extension block

| ioreg Framebuffer | PortID | AlphanumericSerialNumber | Product |
|-------------------|--------|--------------------------|---------|
| `dispext0` | 32 | `ABCDEF012345` | VG28UQL1A |
| `dispext1` | 209768448 | `9876543210123` | VG28UQL1A |
| `dispext2` | 209772544 | *(none)* | ASUS PG32UQ |

The Thunderbolt port metadata also contains EDID with these serials, along with `ParentPortNumber` (physical USB-C port) and `Tunneled` (whether routed through a Thunderbolt tunnel).

**Problem**: There was no known way to map from `dispext0`/`dispext1` back to the contextual display IDs (`2`/`5`).

### 4. CoreGraphics (via ctypes)

`CGGetActiveDisplayList` provides `CGDisplayID` (= contextual ID) and `CGDisplayUnitNumber`, but:

- `CGDisplaySerialNumber` returns the same `111222333` for both C screens
- `CGDisplayIOServicePort` returns `0` on Apple Silicon — the bridge between CoreGraphics and IOKit is fully dead
- `CGDisplayUnitNumber` values (1, 4) don't map to `dispext` indices (0, 1) in any consistent formula

### 5. CoreDisplay Private Framework (the breakthrough)

`CoreDisplay_DisplayCreateInfoDictionary(CGDisplayID)` returns a `CFDictionary` per display that includes **`IODisplayLocation`** — the full IOKit registry path to the framebuffer:

| Contextual ID | IODisplayLocation |
|---------------|-------------------|
| 4 | `IOService:/.../dispext2@AA000000/AppleCLCD2` |
| 2 | `IOService:/.../dispext0@BB000000/AppleCLCD2` |
| 5 | `IOService:/.../dispext1@CC000000/AppleCLCD2` |

This closes the loop:

```
CGDisplayID (contextual ID)
  → CoreDisplay_DisplayCreateInfoDictionary()
    → IODisplayLocation → dispext index
      → ioreg AppleCLCD2 DisplayAttributes
        → AlphanumericSerialNumber (unique hardware serial)
```

## Complete Identification Chain

```
contextual_id=2  →  IODisplayLocation=.../dispext0  →  AlphaSerial="ABCDEF012345"
contextual_id=5  →  IODisplayLocation=.../dispext1  →  AlphaSerial="9876543210123"
```

This chain can be queried at runtime using:

1. **ctypes** to call `CoreDisplay_DisplayCreateInfoDictionary(ctx_id)` — extract `IODisplayLocation`
2. **ioreg** (`ioreg -r -c AppleCLCD2 -d 1 -a`) parsed as plist — extract `AlphanumericSerialNumber` per `IONameMatched` (dispext index)

No pyobjc dependency is required; `ctypes` + CoreFoundation helpers suffice.

## Implications for display-layout-manager.py

With this chain, the `KnownScreen` registry for C screens can include the unique `AlphanumericSerialNumber`:

- `C1` = the physical monitor with serial `ABCDEF012345` (always the left screen)
- `C2` = the physical monitor with serial `9876543210123` (always the right screen)

Instead of offering two layout permutations and asking the user to pick, the script can:

1. Query the identification chain to determine which contextual ID maps to which physical monitor
2. Automatically select the correct layout
3. Apply it without ambiguity

## Caveats

- `CoreDisplay_DisplayCreateInfoDictionary` is a **private API** — it could break in a future macOS update, though it has been stable across macOS 12–15.
- The `AlphanumericSerialNumber` is read from the EDID extension block. Monitors that don't populate this field would need a different distinguishing strategy (PortID, Tunneled status, etc.).
- The ioreg query (`ioreg -r -c AppleCLCD2 -d 1 -a`) takes ~3-4 seconds due to IOKit enumeration overhead.

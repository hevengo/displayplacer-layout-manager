# Display Port Recovery Status

Last updated: 2026-04-27 22:46:18 CEST

## Purpose

This report captures the current state and observations around the `center-32`
display wake/link problem so the investigation can be resumed the next time the
failure is reproducible.

## Current Hardware State

Current read-only snapshot from `./display-port-recovery-diagnostics.py snapshot`:

- `center-32` / ASUS PG32UQ is visible as display id `3`.
- displayplacer reports id `3` as enabled, main, `2560x1440 @ 60Hz`.
- CoreGraphics / CGS reports id `3` as active, online, main.
- CoreDisplay maps id `3` to `dispext2`.
- AppleCLCD2 reports `dispext2` as `AUS ASUS PG32UQ`, manufacture `2022-W3`.
- The MacBook panel is id `1`.
- The right ASUS VG28UQL1A is id `2`.
- The left ASUS VG28UQL1A is id `5`.

USB / USB-C display-adapter candidates from the same snapshot:

- `location=1245184 (0x130000)`:
  Microsoft Surface USB-C to DisplayPort Adapter,
  `idVendor=0x45e`, `idProduct=0x956`.
- `location=1310720 (0x140000)`:
  Microsoft Surface USB-C to DisplayPort Adapter,
  `idVendor=0x45e`, `idProduct=0x956`.
- `location=17825792 (0x1100000)`:
  Synaptics VMM7100,
  `idVendor=0x6cb`, `idProduct=0x7100`.
- `location=1146880 (0x118000)`:
  CalDigit Element Hub billboard/control device.

Physical adapter note:

- The USB-C to HDMI adapter in use is a Cable Matters branded adapter.
- The adapter may still appear in IOKit under the internal bridge-chip vendor
  rather than the retail brand.
- Given the brand and symptom, the `Synaptics VMM7100` entry is the most likely
  USB-C/HDMI bridge candidate to investigate first.
- The two `Microsoft Surface USB-C to DisplayPort Adapter` entries may be other
  USB-C display devices, stale naming from the bridge firmware, or unrelated
  adapters. Confirm by dry-run/matching before resetting either one.

The display came back online while only read-only or dry-run diagnostics were
being run. No actual reset or display toggle was executed.

## Problem States Observed

Earlier state, before the adapter/display connection was rearranged:

- `center-32` was detected as disabled but connected.
- Private CGS re-enable failed:
  `stage=complete_configuration`, `rc=1001`.
- displayplacer fallback failed because displayplacer could not find screen `3`.
- The layout manager now aborts cleanly instead of crashing:
  `Cannot apply layout: required display(s) not active: center-32`.

Later state, after reconnecting/reordering ports:

- `display-layout-manager.py` and displayplacer showed four active displays.
- `center-32` appeared enabled and main in macOS.
- The monitor itself still reported no HDMI signal.
- This means the failure can be a half-working link state:
  macOS believes the display pipeline is active, while the adapter/monitor link
  is not actually producing a usable signal.

## Commands Run When The Display Came Back

These commands were run before the display unexpectedly recovered. They were
read-only or dry-run only:

```bash
ioreg -r -c AppleUSBHostDevice -d 2 -a | head -c 1200
ioreg -r -c IOThunderboltPort -d 2 -a | head -c 1000
ioreg -r -c AppleCLCD2 -d 1 -a | head -c 1000
```

After creating the isolated diagnostic tool:

```bash
./display-port-recovery-diagnostics.py usb-reset --location 0 --dry-run
./display-port-recovery-diagnostics.py snapshot | sed -n '1,220p'
./display-port-recovery-diagnostics.py snapshot | sed -n '/Suggested experiment ladder/,$p'
./display-port-recovery-diagnostics.py reapply-mode --id 3 --dry-run
./display-port-recovery-diagnostics.py snapshot
```

Important: `usb-reset --location 0 --dry-run` only compiled the temporary
IOUSBLib helper and looked for a USB device at location `0`. It found no device
and did not call `ResetDevice`, `USBDeviceReEnumerate`, or open any adapter.

Also not run:

- No `displayplacer-toggle --yes`.
- No `cgs-toggle --yes`.
- No `mode-cycle --yes`.
- No real `usb-reset --yes`.
- No WindowServer restart.

The dry-run `reapply-mode` command printed this command but did not execute it:

```bash
displayplacer "id:3 res:2560x1440 hz:60 color_depth:8 enabled:true scaling:on origin:(0,0) degree:0"
```

## Working Hypotheses

- The problem may be link training or adapter wake state rather than normal
  display layout state.
- macOS can report the display as active and online while the monitor reports no
  signal.
- Walking the display/USB registry or asking displayplacer/CoreGraphics for
  state may have coincided with, or indirectly nudged, re-enumeration.
- The Synaptics VMM7100 entry is now the most interesting USB reset target
  because the physical adapter is a Cable Matters USB-C to HDMI adapter and the
  VMM7100 looks like an adapter bridge-chip identity.
- The Microsoft Surface adapter entries remain worth checking with dry-run, but
  should not be assumed to be the Cable Matters adapter without confirmation.
- The CalDigit hub entries should be treated as lower-priority because resetting
  a hub is broader and riskier.

## Diagnostic Tool Added

New isolated tool:

```bash
./display-port-recovery-diagnostics.py
```

Default command is `snapshot`, which is read-only.

Useful read-only commands:

```bash
./display-port-recovery-diagnostics.py snapshot
./display-port-recovery-diagnostics.py snapshot --json
./display-port-recovery-diagnostics.py sls-detect
./display-port-recovery-diagnostics.py iokit-probe
./display-port-recovery-diagnostics.py reapply-mode --id 3 --dry-run
./display-port-recovery-diagnostics.py usb-reset --location 17825792 --dry-run
```

Guarded experimental commands:

```bash
./display-port-recovery-diagnostics.py reapply-mode --id 3
./display-port-recovery-diagnostics.py ddc-dpms-cycle --display 'ASUS PG32UQ' --yes
./display-port-recovery-diagnostics.py sleep-displays --yes
./display-port-recovery-diagnostics.py displayplacer-toggle --id 3 --yes
./display-port-recovery-diagnostics.py cgs-toggle --id 3 --yes
./display-port-recovery-diagnostics.py mode-cycle --id 3 --temporary-res 1920x1080 --temporary-hz 60 --yes
./display-port-recovery-diagnostics.py displaypolicyd-restart --yes
./display-port-recovery-diagnostics.py usb-reset --location 17825792 --yes
```

The `sls-detect` command calls SkyLight's private `SLSDetectDisplays()` soft
reprobe. This is believed to be the same low-level action behind macOS
"Detect Displays" and is now the first software nudge to try.

The `iokit-probe` command calls `IOServiceRequestProbe` against `AppleCLCD2`
and `AppleDisplay` services. It is read-only from a layout perspective but asks
the display services to reprobe.

The USB reset command compiles a temporary C helper that can use legacy IOUSBLib
to attempt `ResetDevice`, `USBDeviceReEnumerate`, or open/close on a USB device
matched by `locationID`.

## Next Reproduction Checklist

When the monitor says no signal but macOS still shows the display:

1. Do not unplug anything yet.
2. Record exact time and monitor input message.
3. Capture a read-only snapshot:

```bash
mkdir -p captures
./display-port-recovery-diagnostics.py snapshot --json > "captures/display-port-recovery-$(date +%Y%m%d-%H%M%S).json"
./display-port-recovery-diagnostics.py snapshot | tee "captures/display-port-recovery-$(date +%Y%m%d-%H%M%S).txt"
```

4. Confirm the current contextual display id for `center-32`; it is currently
   `3`, but contextual ids can change.
5. Try the least invasive nudges first:

```bash
./display-port-recovery-diagnostics.py sls-detect
./display-port-recovery-diagnostics.py reapply-mode --id 3
```

6. If macOS still reports the display but the monitor still says no signal, try
   monitor-side and display-service nudges:

```bash
./display-port-recovery-diagnostics.py ddc-dpms-cycle --display 'ASUS PG32UQ' --yes
./display-port-recovery-diagnostics.py sleep-displays --yes
./display-port-recovery-diagnostics.py iokit-probe
./display-port-recovery-diagnostics.py mode-cycle --id 3 --temporary-res 1920x1080 --temporary-hz 60 --yes
```

7. If still broken, identify the adapter candidate with dry-run first:

```bash
./display-port-recovery-diagnostics.py usb-reset --location 17825792 --dry-run
./display-port-recovery-diagnostics.py usb-reset --location 1245184 --dry-run
./display-port-recovery-diagnostics.py usb-reset --location 1310720 --dry-run
```

8. Only after confirming which `locationID` corresponds to the physical adapter,
   try the real reset:

```bash
./display-port-recovery-diagnostics.py usb-reset --location 17825792 --yes
```

9. If still broken, restart `displaypolicyd` before the heavier WindowServer
   option:

```bash
./display-port-recovery-diagnostics.py displaypolicyd-restart --yes
```

10. If software nudges fail, the remaining escalation is:

```bash
./display-port-recovery-diagnostics.py windowserver-restart --yes
```

This logs out the GUI session.

## Verification Status

The diagnostic tool and existing layout manager compile:

```bash
uv run --with ruamel.yaml --with rumps python -m py_compile display-layout-manager.py display-port-recovery-diagnostics.py tests/test_reenable_recovery.py tests/test_port_recovery_diagnostics.py
```

Unit tests pass:

```bash
uv run --with ruamel.yaml --with rumps python -m unittest discover
```

Result at implementation time: `Ran 9 tests ... OK`.

## Repo State Notes

At the time of this report, the relevant untracked additions are:

- `display-port-recovery-diagnostics.py`
- `tests/__init__.py`
- `tests/test_port_recovery_diagnostics.py`
- `display-port-recovery-status.md`

No git branch switch, staging, or commit was performed.

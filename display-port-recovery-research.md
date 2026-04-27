# Display Port Recovery — Research Report

Last updated: 2026-04-27

## Purpose

This report captures research into low-level / private / undocumented macOS APIs
that could be used to recover a wedged DisplayPort Alt-Mode link on Apple
Silicon (M1 Max, macOS 26.3.1) without physically replugging the cable.

It complements [display-port-recovery-status.md](display-port-recovery-status.md),
which documents the observed problem states and the diagnostic tool already in
the repo. Read that file first; this file is the "what else can we try and
why" companion.

## Context Recap

- Hardware: 4 displays — MacBook panel, two ASUS VG28UQL1A 28", one ASUS PG32UQ
  32" (`center-32`).
- `center-32` is connected through a USB-C → DisplayPort/HDMI adapter chain:
  Microsoft Surface USB-C to DisplayPort Adapter, Synaptics VMM7100,
  `AppleUSBHostBillboardDevice` with `UsbBillboardCurrentMode = DisplayPort`.
- Two failure modes observed:
  1. macOS reports the display as disabled. Private CGS re-enable fails with
     `stage=complete_configuration`, `rc=1001`. displayplacer fallback fails
     with "Unable to find screen 3".
  2. macOS reports the display as enabled, online and main, but the monitor
     itself shows no signal. The pipeline is half-working.
- `rc=1001` is `kCGErrorRangeCheck` from `CGError.h`. CGS got a configuration
  descriptor referencing a display id that WindowServer no longer has in its
  display list. Matches "WindowServer-side object gone, IOFB stub
  half-alive."

## Headline Finding

**There is no public or private user-space API on Apple Silicon that resets a
wedged USB-C DisplayPort Alt-Mode link.** That layer lives in `AppleHPM`
firmware and `IOAccessoryPortAppleSilicon`, both entitlement-locked behind
`com.apple.private.iousbhost.allow-any-iousbhost-driver`. `USBDeviceReEnumerate`,
`tbtutil`, `usbreset`, and `displayreconfigure` either no longer exist or were
never shipped on M-series. Asahi Linux's USB-C team has confirmed this with
independent reverse engineering.

The single highest-value undocumented call we are not yet using is
`SLSDetectDisplays`. It is the API that Option-clicking "Detect Displays" in
System Settings → Displays actually invokes.

## Confirmed Symbols on This Machine

Verified by `strings` / `dyld_shared_cache_util` against
`/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/dyld_shared_cache_arm64e`
on macOS 26.3.1.

Present in `SkyLight.framework`:

```
_SLSDetectDisplays                   ← the prize
_SLSBeginDisplayConfiguration
_SLSCompleteDisplayConfiguration
_SLSCompleteDisplayConfigurationWithOption
_SLSConfigureDisplayEnabled
_SLSConfigureDisplayMode
_SLSConfigureDisplayMirrorOfDisplay
_SLSConfigureDisplayOrigin
_SLSConfigureDisplayResolution
_SLSConfigureDisplayOutputMode
_SLSConfigureDisplayIndependentOutput
_SLSCancelDisplayConfiguration
_SLSCaptureDisplay
_SLSDisplayChangedSeed
_SLSDisplayFactoryReset
_SLSCopyDisplayInfoDictionary
```

CGS notification constants:

```
kCGSDisplayPrepareForReprobing       ← posted before "Detect Displays"
kCGSDisplayWillReconfigure
kCGSDisplayDidReconfigure / DidReconfigure2
kCGSDisplayHardwareChanged
kCGSDisplayConfigEnable
kCGSDisplayConfigMode / ConfigOrigin / ConfigMirror
```

Present in `IOKit.framework`:

```
_IOServiceRequestProbe
_IOFBSetDisplayModeAndDepth
_IOFBSetStartupDisplayModeAndDepth
_IOAVServiceReadI2C
_IOAVServiceWriteI2C
_IOAVServiceRetrainFRL              ← HDMI 2.1 FRL only, not DisplayPort
```

Not present anywhere on this machine:

- Any symbol containing `ReEnumerate`, `Renegotiate`, `RescanDisplays`,
  `ForceDetect`.
- Binaries `tbtutil`, `displayreconfigure`, `fbreset`, `usbreset`.

## Recovery Vectors, Ranked

Each vector below is software-only, low blast radius, and reversible.

### 1. `SLSDetectDisplays()` — the soft re-plug

The closest macOS-shipped equivalent to a soft re-plug. Posts
`kCGSDisplayPrepareForReprobing`, drops into `IOServiceRequestProbe` against
the AppleDisplay objects, triggers an IOFB rescan.

```python
import ctypes
sl = ctypes.CDLL("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight")
sl.SLSDetectDisplays.restype = ctypes.c_int
sl.SLSDetectDisplays.argtypes = []
err = sl.SLSDetectDisplays()
```

- No entitlement required. Works under SIP. Callable from a daemon as user or
  root. AMFI does not block dlopen of SkyLight.
- Will recover: WindowServer lost the display while DCP still has it. This is
  exactly the rc=1001 / "Unable to find screen 3" pattern.
- Will not recover: the link is wedged below the SoC and HPD has not been
  raised. In that case there is nothing new to detect.

Pair with a forced reconfiguration transaction to maximize chance of a rescan:

```python
sl.SLSBeginDisplayConfiguration.restype = ctypes.c_int
sl.SLSBeginDisplayConfiguration.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
cfg = ctypes.c_void_p()
sl.SLSBeginDisplayConfiguration(ctypes.byref(cfg))
sl.SLSCompleteDisplayConfigurationWithOption.argtypes = [ctypes.c_void_p, ctypes.c_int]
sl.SLSCompleteDisplayConfigurationWithOption(cfg, 2)   # 2 = kCGConfigurePermanently
sl.SLSDetectDisplays()
```

### 2. DDC/CI DPMS cycle on the PG32UQ

Use [m1ddc](https://github.com/waydabber/m1ddc) to send VESA DPMS off then on
to the monitor. The PG32UQ has a robust DDC implementation and its own DPMS
handler cycles its DP receiver, which usually pulls HPD low/high so the source
side can re-train.

```bash
m1ddc display 'ASUS PG32UQ' set 0xD6 4   # DPMS off
sleep 2
m1ddc display 'ASUS PG32UQ' set 0xD6 1   # DPMS on
sleep 1
# then SLSDetectDisplays again
```

Specifically attested to work on this monitor model. ~3 s blackout cost.
Effective for the "macOS thinks display is on, monitor shows no signal" state.

### 3. Display sleep + synthetic wake

Drops DCP's framebuffer assertion. On wake, DCP re-queries
`IOAccessoryPort` Alt-Mode state, which forces the USB-C PD controller to
re-evaluate. Most-reported empirical fix on Apple discussion forums for
DP-Alt wedges.

```bash
/usr/bin/pmset displaysleepnow && sleep 2 && /usr/bin/caffeinate -u -t 1
```

Known regression on Tahoe: `caffeinate -u` may not wake DCP when it has gone
to deep framebuffer-off state. Pair with a synthetic mouse event from the
daemon to be robust:

```python
# CGEventCreateMouseEvent + CGEventPost
```

### 4. Bounce `displaypolicyd`

`displaypolicyd` is the Apple Silicon replacement for AGDC. On respawn it
re-queries DCP for the display list.

```bash
sudo killall -9 displaypolicyd
```

Safe — does not log out the user, unlike `killall WindowServer`. Modest hit
rate but very cheap. launchd respawns automatically.

### 5. Toggle a healthy display's mode and back

Set the MacBook built-in to a non-default scaled mode for one configuration
transaction, then restore. SkyLight rescans all online displays as part of
`CGCompleteDisplayConfiguration`. Often re-attaches a wedged sibling display.

This is essentially what step (1) does explicitly, but performed implicitly via
a real configuration commit. Worth trying as a fallback when (1) returns
without effect.

## Vector Details

### CoreGraphics / SkyLight private CGS APIs

In modern macOS the public `CG*` display APIs in CoreGraphics.framework are
thin wrappers around SkyLight
(`/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight`). The private
surface is the `SLS*` family with `CGS*` aliases retained for legacy
compatibility. On Tahoe SkyLight only exists inside the dyld shared cache;
there is no on-disk dylib.

`_SLSDetectDisplays` is undocumented private SPI. It is not in NSHipster's
`CGSPrivate.h` mirror or in `phracker/MacOSX-SDKs`. The most accurate floating
header is in BetterDisplay-adjacent source, where it is re-declared as
`extern void SLSDetectDisplays(void);` — some builds appear to return
`CGError`. It is called by WindowServer proper near `gCGXDisplayReconfigureState`.

Likelihood: high for the "WindowServer lost the display but IOFB is alive"
subset (the rc=1001 / "Unable to find screen 3" pattern). Medium-low for a
wedged DP-Alt link where DCP has not seen HPD raised — `SLSDetectDisplays`
ultimately drops into `kCGSDisplayPrepareForReprobing` →
`IOServiceRequestProbe` on the AppleDisplay objects → IOFB rescan; if HPD
never fires, there is no new mode-list to find. Still the cheapest, lowest-
blast-radius first move.

Apple Silicon caveats: works under SIP. No entitlement needed because
SkyLight is loadable by any process. CGS *writes* from a sandboxed app are
silently dropped by WindowServer; LaunchDaemons running as root are fine.
AMFI does not gate dlopen of SkyLight. arm64e PAC signing has no effect on
dlsym'd symbols since they are AUTH-resolved at bind time inside the
framework.

### IOKit display reset paths

The IOFB / IODisplayConnect / IODisplayWrangler ladder. `IOServiceRequestProbe`
is in IOKit.framework's public surface. The IOFB ones
(`IOFBSetDisplayModeAndDepth`, `IOFBSetStartupDisplayModeAndDepth`) are
exposed but undocumented.

Likelihood on Apple Silicon: low to medium. On Intel, `IODisplayWrangler`
and `IOFramebuffer` were the kernel-level handles. On M-series the
framebuffer is owned by DCP (Display Coprocessor running its own RTKit
firmware) and the kernel-side `IOFramebuffer` shim is much thinner.
`IOServiceRequestProbe` against `IODisplayConnect` can still kick a re-probe;
it propagates into DCP via `AppleCLCD2` / `AppleDCPExtService`. It will not
fix anything upstream of the SoC because DCP can only see what the USB-C PHY
/ retimer hands it.

Example (no shell command for this — bindings only):

```c
io_iterator_t it;
IOServiceGetMatchingServices(kIOMainPortDefault,
    IOServiceMatching("AppleDisplay"), &it);
for (io_service_t s; (s = IOIteratorNext(it));) {
    IOServiceRequestProbe(s, kIOFBUserRequestProbe);   // 0x00000000
    IOObjectRelease(s);
}
```

`kIOFBUserRequestProbe` is in `IOKit/graphics/IOGraphicsTypes.h`. On Apple
Silicon target `AppleCLCD2` rather than `IODisplay`. `kIOFBClamshellState` is
gone on M-series — clamshell is implemented inside DCP. Sealed System Volume
prevents patching; SIP blocks `kextload` of replacement drivers.

### AppleGraphicsControl / AGDC

`AppleGraphicsControl.kext` was the Intel/discrete-GPU policy daemon. On
Apple Silicon there is no AGDC — the equivalent is
`/usr/libexec/displaypolicyd`, a thin user-space policy daemon talking to DCP.
`kextstat -l | grep -i AppleGraphicsControl` returns nothing on M1.
AGDCDiagnose / AGDCPolicyEvent tricks are dead.

Useful for diagnostics only:

```bash
sudo log stream --level=debug --predicate \
  'subsystem == "com.apple.AppleGraphicsControl" OR subsystem == "com.apple.displaypolicyd"'
```

The actually-useful subsystems on M-series are
`com.apple.iokit.IOMobileFramebuffer`, `com.apple.AppleCLCD2`, and
`subsystem CONTAINS "DCP"`.

### Power management vectors

`pmset displaysleepnow` requires no privileges. `pmset sleepnow` requires
admin.

A more aggressive variant — system sleep then scheduled wake — does work but
is disruptive:

```bash
sudo pmset schedule wake "$(date -v +20S '+%m/%d/%y %H:%M:%S')"
pmset sleepnow
```

The IOPMrootDomain user client (`IOPMAssertionCreateWithName`,
`IOPMSchedulePowerEvent`) is the underlying API.
`pmset -g log` is useful for diagnosing.

### USB / Thunderbolt private interfaces

What you can probe from user space (read-only):

```bash
ioreg -rc IOAccessoryPortAppleSilicon -l | rg -A2 'class IOAccessoryPortAppleSilicon'
```

Property reads succeed; writes fail without entitlement.

Specific facts on Apple Silicon:

- `USBDeviceReEnumerate`: gone from public IOUSBHost user clients on
  M-series. Symbol absent from shared cache.
- `IOUSBHostDevice::reset()` and `abortDeviceRequests()` exist kernel-side
  but are not reachable from user space — user-client method dispatch
  table pruned.
- `IOAccessoryPortAppleSilicon` (kernel class owning USB-C PD/Alt-Mode
  negotiation, including `AppleHPM`, `AppleTypeCPort`): user client is
  Apple-internal, gated by
  `com.apple.private.iousbhost.allow-any-iousbhost-driver`.
- `AppleTypeCRetimer` / `AppleTypeCPhy`: present at
  `/usr/lib/updaters/libAppleTypeCRetimerUpdater.dylib`. These are
  firmware-update DFU paths, not runtime renegotiation knobs.
- `tbtutil`: not shipped on Apple Silicon (was at `/usr/sbin/tbtutil` on
  Intel). `IOThunderboltFamily` is loaded but its user client is
  entitlement-gated.
- DPCD over AUX channel: no user-space access on M-series. On Intel you
  could poke DPCD via `IOFBI2C` / AUX through `IOFramebufferI2CRequest`,
  but DCP does not expose an equivalent.
- `IOAVServiceReadI2C` / `IOAVServiceWriteI2C` — DDC/CI side-channel, not
  AUX; cannot retrain DP link.
- `IOAVServiceRetrainFRL` — HDMI 2.1 FRL retraining only.

Asahi Linux's USB-C team (Sven Peter, Hector Martin) has confirmed via
independent RE that the AppleHPM (PD controller) firmware is closed and the
only kernel actor with access is `AppleHPM` / `IOAccessoryPortAppleSilicon`.
References:

- https://asahilinux.org/2022/11/november-2022-report/
- https://github.com/AsahiLinux/docs/wiki/HW%3AUSB-C
- https://github.com/AsahiLinux/linux/tree/asahi/drivers/soc/apple/rtkit-helper

Kext locations:

- `/System/Library/Extensions/IOUSBHostFamily.kext`
- `/System/Library/Extensions/AppleTypeCRetimer.kext`
- `/System/Library/Extensions/AppleHPM.kext`

### DriverKit / dext options

Third-party USB DriverKit (`USBDriverKit.framework`) is the only legitimate
user-space USB path on Apple Silicon. To attach a dext to the
USB-C → DP adapter you need:

- `com.apple.developer.driverkit` entitlement — provisioning-profile-only,
  requires special developer ID and Apple approval.
- `com.apple.developer.driverkit.transport.usb` matching dictionary
  targeting the Surface adapter's VID/PID (`0x45e:0x956`).

Even with a dext attached, DP-Alt-Mode renegotiation lives in `AppleHPM`
firmware, not in the USB device endpoint. A dext can `->reset()` the USB
device endpoint (the billboard endpoint), which can anecdotally kick the
chain enough that AppleHPM re-runs PD discovery. Reports of this working
specifically with VMM7100-based adapters are rare. Realistic probability:
20–30%, plus a multi-week Apple-approval gate.

SIP blocks unsigned dexts. AMFI rejects `*.dext` bundles without a
properly-signed `com.apple.developer.driverkit` entitlement.

Reference: https://developer.apple.com/documentation/usbdriverkit

### Underlying behavior of common tools

- `displayplacer` calls `CGBeginDisplayConfiguration` →
  `CGConfigureDisplayWithDisplayMode` / `Origin` /
  `MirrorOfDisplay` → `CGCompleteDisplayConfiguration`. It does not call
  `SLSDetectDisplays`. That is exactly why it returns "Unable to find
  screen 3" when WindowServer's display list does not contain id 3 —
  `CGGetOnlineDisplayList` does not see it.
- `cscreen`, `screenresolution` — old projects, also pure public CG.
- `dccmd` — does not exist on macOS Apple Silicon.

## Vectors That Do Not Help

Documented for the record so we do not waste time on them again.

- `killall -HUP WindowServer` /
  `launchctl kickstart -k system/com.apple.WindowServer`
  → terminates the user's loginwindow session. Do not use.
- `tbtutil`, `displayreconfigure`, `fbreset`, `usbreset`
  → not shipped on Apple Silicon. Verified absent on disk and in shared cache.
- AGDC tricks (`AGDCDiagnose -F`, AGDCPolicyEvent, etc.)
  → AGDC kext is not loaded on M-series. Replaced by `displaypolicyd`.
- `IOFBSetStartupDisplayMode`, `IODisplayWrangler` user clients
  → mostly no-ops on DCP-backed framebuffers. The framebuffer is owned by
  the DCP coprocessor; the kernel-side `IOFramebuffer` shim is now thin.
- `IOAVServiceRetrainFRL`
  → HDMI 2.1 Fixed Rate Link retraining only. Useless for DisplayPort.
- `IOAVServiceReadI2C` / `IOAVServiceWriteI2C` for AUX
  → these are the DDC/CI side-channel, not the DP AUX channel. Cannot retrain
  DP link from DDC.
- DPCD-over-AUX from user space
  → not exposed on M-series. DCP does not surface an AUX user client.
- `IOAccessoryPortAppleSilicon` user client
  → entitlement-gated by
  `com.apple.private.iousbhost.allow-any-iousbhost-driver`. AMFI rejects
  unentitled callers.
- USBDriverKit dext targeting the Surface adapter
  → 20–30% chance of helping at best, plus an Apple developer-approval gate
  for the `com.apple.developer.driverkit` entitlement. Not pursued.
- `USBDeviceReEnumerate` from `IOUSBDeviceInterface300`
  → user-client method dispatch table pruned on Apple Silicon. Symbol
  absent from shared cache. Kernel side `IOUSBHostDevice::reset()` exists
  but is unreachable from user space without special entitlements.

## Why the Display Came Back On Its Own (2026-04-27 Session)

During the diagnostic session the display recovered without any explicit
reset action. Only read-only commands were issued (`strings`, `ls`, `ioreg`,
`which`, `kextstat`, snapshot dumps). Likely triggers:

- DCP re-evaluating Alt-Mode state on some idle/wake transition.
- A brief HPD pulse from the adapter.
- A DDC poll from another tool kicking the monitor's DPMS state.
- IOKit registry walks (`ioreg -rc AppleCLCD2 -d 1`,
  `ioreg -r -c AppleUSBHostDevice -d 2`) potentially nudging matching
  drivers to refresh state, though no documented side effects of `ioreg`
  read calls are known.

To capture the trigger next time, run this beforehand:

```bash
sudo log stream --level=debug --predicate \
  'subsystem CONTAINS[c] "DCP" OR subsystem == "com.apple.iokit.IOMobileFramebuffer" OR subsystem == "com.apple.displaypolicyd"'
```

## Suggested Next-Reproduction Ladder

When the failure recurs, in order of cost:

1. Capture a snapshot first (read-only, see status doc):
   ```bash
   ./display-port-recovery-diagnostics.py snapshot --json > "captures/$(date +%Y%m%d-%H%M%S).json"
   ```
2. Start a `log stream` in the background as above.
3. Call `SLSDetectDisplays()` via a small Python helper.
   Re-run `CGGetOnlineDisplayList`. If `center-32` appears, attempt the layout
   apply normally.
4. If display still missing or shows "no signal", DDC DPMS cycle via m1ddc.
5. If still broken, `pmset displaysleepnow` + synthetic wake.
6. If still broken, `sudo killall -9 displaypolicyd`.
7. If still broken, mode-cycle a healthy display (built-in panel).
8. If still broken at this point, the link is wedged below the SoC's reach.
   Only physical replug, port-power-cycle of the dock, or a full shutdown
   will recover it. There is no documented or undocumented user-space
   USB-C / Thunderbolt / DP-Alt-Mode re-enumeration API on Apple Silicon.

## Suggested Implementation Work

- Add an `slsdetect` (or extend the existing diagnostic tool) command that
  calls `SLSDetectDisplays()`. Cheap, no flag guard needed; this is what
  System Settings does.
- Add a `recover` orchestrator that runs the ladder above, with a
  `CGGetOnlineDisplayList` re-check between each step and clear log lines
  saying which step succeeded.
- Add a `diagnose` command that prints, in a single report:
  - displayplacer active displays
  - CGS disabled displays
  - `ioreg AppleUSBHostBillboardDevice` summary
  - matching USB-C adapter names / locationIDs
  - Thunderbolt/Element Hub state
  - whether HPD log lines were observed during the most recent
    sleep/wake cycle
- When integrating into `display-layout-manager.py`'s daemon path: on
  rc=1001, call `SLSDetectDisplays()` first and retry once before reporting
  "required display(s) not active".

## Hardware-Side Notes

If software recovery keeps failing for `center-32`:

- Prefer USB-C → DisplayPort 1.4 directly over USB-C → HDMI for the
  PG32UQ. HDMI requires HDMI 2.1 FRL for full bandwidth and that path is
  more fragile across Alt-Mode wakes.
- Test `center-32` at 60 Hz instead of higher refresh rates if
  bandwidth / link-negotiation is suspected.
- The two Microsoft Surface adapters at locations `0x130000` and `0x140000`
  plus the Synaptics VMM7100 at `0x1100000` are the likely physical chain.
  The VMM7100 is the DP-to-HDMI protocol converter; if the issue is on the
  HDMI side of the chain, switching to a pure DP cable removes that chip
  from the picture entirely.

## Reference Open-Source Projects

| Project | URL | Useful for |
|---|---|---|
| Lunar | https://github.com/alinpanaitiu/Lunar | Most thorough community CGS/SkyLight reverse engineering. See `Lunar/DDC/CoreDisplay.swift` and `Lunar/DDC/SkyLight.swift`. |
| BetterDummy | https://github.com/waydabber/BetterDummy | Open-source sibling of BetterDisplay. Good `SLSDetectDisplays` usage pattern. |
| MonitorControl | https://github.com/MonitorControl/MonitorControl | Pure DDC/CI via `IOAVServiceCreateWithService`. Template for DPMS cycling over DDC. |
| m1ddc | https://github.com/waydabber/m1ddc | One-binary DDC for Apple Silicon. |
| displayplacer | https://github.com/jakehilborn/displayplacer | Source confirms it uses only public CG calls — does not reprobe. |
| CGSInternal | https://github.com/NUIKit/CGSInternal | Floating header collection for private CGS APIs. |
| Asahi USB-C wiki | https://github.com/AsahiLinux/docs/wiki/HW%3AUSB-C | Authoritative on what AppleHPM/DCP look like internally. |
| Asahi DCP RE | https://github.com/AsahiLinux/m1n1/tree/main/proxyclient/m1n1/hw/dcp | Live RE of DCP's RTKit IPC. |

## Useful Symbol-Mining Commands

For revisiting the shared cache after macOS updates:

```bash
SHARED=/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/dyld_shared_cache_arm64e

# Quick-and-dirty:
strings -a "$SHARED" | rg '^_(SLS|CGS)' | sort -u

# Better, using Xcode CLT:
xcrun /Applications/Xcode.app/Contents/Developer/usr/bin/dyld_shared_cache_util \
    -extract /tmp/dsc-out "$SHARED"
nm -gU /tmp/dsc-out/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight \
    | rg 'SLSDetect|SLSConfigure|SLSCompleteDisplay'

# Find which binaries call _SLSDetectDisplays:
rg -lF 'SLSDetectDisplays' /System/Library /usr/libexec /Applications/Utilities
```

## Open Questions for Next Session

- Does calling `SLSDetectDisplays()` while the link is in the half-working
  state (macOS reports active, monitor shows no signal) succeed in tearing
  the stale state down? Untested — the failure was not reproducible at
  the time of this report.
- Does the DDC DPMS cycle work on this PG32UQ unit? Untested.
- Does `killall -9 displaypolicyd` on its own recover from the rc=1001
  state? Untested.
- Is there an observable log line from `displaypolicyd` or DCP at the moment
  the display spontaneously came back? Capturing that during the next
  failure window is the highest-value diagnostic.

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Diagnose and experimentally nudge macOS display/USB-C video ports.

This script is intentionally separate from display-layout-manager.py.  It is a
manual lab bench for broken wake/link-training states where macOS reports a
display as active but the monitor or adapter still has no signal.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _compact_process_output(text: str, limit: int = 5) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    unique: list[str] = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    return "\n".join(unique[:limit])


def _parse_uint(value: str) -> int:
    text = value.strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def _fmt_hex(value: int | None) -> str:
    return "unknown" if value is None else f"0x{value:x}"


def _fmt_location(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value} ({value:#x})"


def _require_confirmation(args: argparse.Namespace, what: str) -> bool:
    if getattr(args, "yes", False):
        return True
    print(f"Refusing to run {what} without --yes.", file=sys.stderr)
    print("Run a read-only snapshot first, then repeat with --yes.", file=sys.stderr)
    return False


@dataclass
class CommandResult:
    name: str
    args: list[str]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.return_code == 0

    @property
    def summary(self) -> str:
        detail = _compact_process_output(self.stderr or self.stdout, limit=3)
        base = f"{self.name}: rc={self.return_code} elapsed={self.elapsed_s:.2f}s"
        return f"{base}; {detail}" if detail else base


def run_command(
    name: str,
    args: list[str],
    *,
    timeout: int = 30,
    capture: bool = True,
) -> CommandResult:
    start = time.monotonic()
    try:
        result = subprocess.run(
            args,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
        )
        return CommandResult(
            name,
            args,
            result.returncode,
            result.stdout if capture else "",
            result.stderr if capture else "",
            time.monotonic() - start,
        )
    except FileNotFoundError as exc:
        return CommandResult(name, args, 127, stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            name,
            args,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or f"timed out after {timeout}s",
            elapsed_s=time.monotonic() - start,
        )


# ---------------------------------------------------------------------------
# displayplacer parsing and commands
# ---------------------------------------------------------------------------


FIELD_MAP = {
    "Persistent screen id": "persistent_id",
    "Contextual screen id": "contextual_id",
    "Serial screen id": "serial_id",
    "Type": "type",
    "Resolution": "resolution",
    "Hertz": "hertz",
    "Color Depth": "color_depth",
    "Scaling": "scaling",
    "Origin": "origin",
    "Rotation": "rotation",
    "Enabled": "enabled",
}


@dataclass
class Display:
    persistent_id: str = ""
    contextual_id: str = ""
    serial_id: str = ""
    type: str = ""
    resolution: str = ""
    hertz: str = ""
    color_depth: str = ""
    scaling: str = ""
    origin: str = ""
    rotation: str = ""
    enabled: str = ""

    @property
    def is_active(self) -> bool:
        return self.enabled.lower() == "true" and bool(self.resolution)


def run_displayplacer_list() -> CommandResult:
    return run_command("displayplacer list", ["displayplacer", "list"])


def parse_displays(output: str) -> list[Display]:
    displays: list[Display] = []
    current: Display | None = None

    for line in output.splitlines():
        if line.startswith("Persistent screen id:"):
            current = Display()
            displays.append(current)

        if current is None:
            continue

        for prefix, attr in FIELD_MAP.items():
            if line.startswith(f"{prefix}:"):
                setattr(current, attr, line[len(prefix) + 1 :].strip())
                break

    return displays


def _parse_origin(origin: str) -> tuple[int, int]:
    match = re.search(r"\((-?\d+),\s*(-?\d+)\)", origin)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _display_by_id(displays: list[Display], display_id: int) -> Display | None:
    needle = str(display_id)
    return next((d for d in displays if d.contextual_id == needle), None)


def _displayplacer_arg(
    display: Display,
    *,
    enabled: bool | None = None,
    resolution: str | None = None,
    hertz: int | str | None = None,
) -> str:
    res = resolution or display.resolution
    hz = str(hertz if hertz is not None else display.hertz)
    color_depth = display.color_depth or "8"
    scaling = display.scaling or "on"
    ox, oy = _parse_origin(display.origin)
    enable_text = display.enabled.lower() if enabled is None else str(enabled).lower()
    rotation = display.rotation or "0"
    return (
        f"id:{display.contextual_id}"
        f" res:{res}"
        f" hz:{hz}"
        f" color_depth:{color_depth}"
        f" enabled:{enable_text}"
        f" scaling:{scaling}"
        f" origin:({ox},{oy})"
        f" degree:{rotation}"
    )


def _run_displayplacer_arg(name: str, arg: str) -> CommandResult:
    return run_command(name, ["displayplacer", arg])


def _run_id_enable(name: str, display_id: int, enabled: bool) -> CommandResult:
    value = "true" if enabled else "false"
    return run_command(name, ["displayplacer", f"id:{display_id} enabled:{value}"])


# ---------------------------------------------------------------------------
# CoreGraphics / private CGS probing and nudging
# ---------------------------------------------------------------------------


@dataclass
class CoreGraphicsDisplay:
    display_id: int
    active: bool
    online: bool
    main: bool
    serial: int | None = None
    vendor: int | None = None
    model: int | None = None
    builtin: bool = False
    bounds: str = ""


@dataclass
class CgsResult:
    success: bool
    method: str
    attempted_ids: list[int] = field(default_factory=list)
    failed_stage: str | None = None
    return_code: int | None = None
    message: str = ""

    @property
    def summary(self) -> str:
        status = "succeeded" if self.success else "failed"
        ids = ", ".join(str(i) for i in self.attempted_ids) or "none"
        parts = [f"{self.method} {status}", f"ids={ids}"]
        if self.failed_stage:
            parts.append(f"stage={self.failed_stage}")
        if self.return_code is not None:
            parts.append(f"rc={self.return_code}")
        if self.message:
            parts.append(self.message)
        return "; ".join(parts)


class CGRect(ctypes.Structure):
    _fields_ = [
        ("origin", ctypes.c_double * 2),
        ("size", ctypes.c_double * 2),
    ]


def _load_coregraphics() -> Any | None:
    path = ctypes.util.find_library("CoreGraphics")
    if not path:
        return None
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None


def coregraphics_snapshot() -> list[CoreGraphicsDisplay]:
    cg = _load_coregraphics()
    if cg is None:
        return []

    c_uint32 = ctypes.c_uint32
    max_displays = 32

    cg.CGSGetDisplayList.argtypes = [
        c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
    ]
    cg.CGSGetDisplayList.restype = ctypes.c_int32
    cg.CGGetActiveDisplayList.argtypes = [
        c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplaySerialNumber.argtypes = [c_uint32]
    cg.CGDisplaySerialNumber.restype = c_uint32
    cg.CGDisplayVendorNumber.argtypes = [c_uint32]
    cg.CGDisplayVendorNumber.restype = c_uint32
    cg.CGDisplayModelNumber.argtypes = [c_uint32]
    cg.CGDisplayModelNumber.restype = c_uint32
    cg.CGDisplayIsBuiltin.argtypes = [c_uint32]
    cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
    cg.CGMainDisplayID.argtypes = []
    cg.CGMainDisplayID.restype = c_uint32

    online_set: set[int] = set()
    if hasattr(cg, "CGGetOnlineDisplayList"):
        cg.CGGetOnlineDisplayList.argtypes = [
            c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
        ]
        cg.CGGetOnlineDisplayList.restype = ctypes.c_int32
        online_ids = (c_uint32 * max_displays)()
        online_count = c_uint32(0)
        if cg.CGGetOnlineDisplayList(
            max_displays, online_ids, ctypes.byref(online_count),
        ) == 0:
            online_set = {int(online_ids[i]) for i in range(online_count.value)}

    all_ids = (c_uint32 * max_displays)()
    all_count = c_uint32(0)
    if cg.CGSGetDisplayList(max_displays, all_ids, ctypes.byref(all_count)) != 0:
        return []

    active_ids = (c_uint32 * max_displays)()
    active_count = c_uint32(0)
    if cg.CGGetActiveDisplayList(
        max_displays, active_ids, ctypes.byref(active_count),
    ) != 0:
        return []
    active_set = {int(active_ids[i]) for i in range(active_count.value)}

    main_id = int(cg.CGMainDisplayID())
    bounds_by_id: dict[int, str] = {}
    if hasattr(cg, "CGDisplayBounds"):
        cg.CGDisplayBounds.argtypes = [c_uint32]
        cg.CGDisplayBounds.restype = CGRect
        for i in range(all_count.value):
            did = int(all_ids[i])
            rect = cg.CGDisplayBounds(c_uint32(did))
            bounds_by_id[did] = (
                f"({rect.origin[0]:.0f},{rect.origin[1]:.0f}) "
                f"{rect.size[0]:.0f}x{rect.size[1]:.0f}"
            )

    result: list[CoreGraphicsDisplay] = []
    for i in range(all_count.value):
        did = int(all_ids[i])
        serial = int(cg.CGDisplaySerialNumber(c_uint32(did)))
        vendor = int(cg.CGDisplayVendorNumber(c_uint32(did)))
        model = int(cg.CGDisplayModelNumber(c_uint32(did)))
        if serial == 0 and vendor == 0 and did not in active_set:
            # The layout manager also filters these virtual/phantom entries.
            continue
        result.append(
            CoreGraphicsDisplay(
                display_id=did,
                active=did in active_set,
                online=did in online_set if online_set else did in active_set,
                main=did == main_id,
                serial=serial,
                vendor=vendor,
                model=model,
                builtin=bool(cg.CGDisplayIsBuiltin(c_uint32(did))),
                bounds=bounds_by_id.get(did, ""),
            )
        )
    return result


def cgs_set_enabled(
    display_ids: list[int],
    enabled: bool,
    *,
    permanent: bool = False,
) -> CgsResult:
    if not display_ids:
        return CgsResult(True, "cgs", [], message="no display ids")

    cg = _load_coregraphics()
    if cg is None:
        return CgsResult(
            False, "cgs", list(display_ids),
            failed_stage="load_coregraphics",
            message="CoreGraphics framework not found",
        )

    c_uint32 = ctypes.c_uint32
    c_void_p = ctypes.c_void_p
    cg.CGBeginDisplayConfiguration.argtypes = [ctypes.POINTER(c_void_p)]
    cg.CGBeginDisplayConfiguration.restype = ctypes.c_int32
    cg.CGSConfigureDisplayEnabled.argtypes = [c_void_p, c_uint32, ctypes.c_bool]
    cg.CGSConfigureDisplayEnabled.restype = ctypes.c_int32
    cg.CGCompleteDisplayConfiguration.argtypes = [c_void_p, c_uint32]
    cg.CGCompleteDisplayConfiguration.restype = ctypes.c_int32
    cg.CGCancelDisplayConfiguration.argtypes = [c_void_p]
    cg.CGCancelDisplayConfiguration.restype = ctypes.c_int32

    config = c_void_p()
    rc = int(cg.CGBeginDisplayConfiguration(ctypes.byref(config)))
    if rc != 0:
        return CgsResult(
            False, "cgs", list(display_ids),
            failed_stage="begin_configuration",
            return_code=rc,
        )

    for did in display_ids:
        rc = int(cg.CGSConfigureDisplayEnabled(config, c_uint32(did), enabled))
        if rc != 0:
            cg.CGCancelDisplayConfiguration(config)
            return CgsResult(
                False, "cgs", list(display_ids),
                failed_stage=f"set_enabled:{did}:{enabled}",
                return_code=rc,
            )

    kCGConfigureForSession = 1
    kCGConfigurePermanently = 2
    option = kCGConfigurePermanently if permanent else kCGConfigureForSession
    rc = int(cg.CGCompleteDisplayConfiguration(config, option))
    if rc != 0:
        cg.CGCancelDisplayConfiguration(config)
        return CgsResult(
            False, "cgs", list(display_ids),
            failed_stage="complete_configuration",
            return_code=rc,
        )

    action = "enabled" if enabled else "disabled"
    scope = "permanent" if permanent else "session"
    return CgsResult(True, "cgs", list(display_ids), message=f"{action} ({scope})")


def cgs_empty_transaction(*, permanent: bool = False) -> CgsResult:
    """Commit an empty display configuration to make SkyLight rescan state."""
    cg = _load_coregraphics()
    if cg is None:
        return CgsResult(
            False, "cgs-empty-transaction",
            failed_stage="load_coregraphics",
            message="CoreGraphics framework not found",
        )

    c_void_p = ctypes.c_void_p
    cg.CGBeginDisplayConfiguration.argtypes = [ctypes.POINTER(c_void_p)]
    cg.CGBeginDisplayConfiguration.restype = ctypes.c_int32
    cg.CGCompleteDisplayConfiguration.argtypes = [c_void_p, ctypes.c_uint32]
    cg.CGCompleteDisplayConfiguration.restype = ctypes.c_int32
    cg.CGCancelDisplayConfiguration.argtypes = [c_void_p]
    cg.CGCancelDisplayConfiguration.restype = ctypes.c_int32

    config = c_void_p()
    rc = int(cg.CGBeginDisplayConfiguration(ctypes.byref(config)))
    if rc != 0:
        return CgsResult(
            False, "cgs-empty-transaction",
            failed_stage="begin_configuration",
            return_code=rc,
        )

    kCGConfigureForSession = 1
    kCGConfigurePermanently = 2
    option = kCGConfigurePermanently if permanent else kCGConfigureForSession
    rc = int(cg.CGCompleteDisplayConfiguration(config, option))
    if rc != 0:
        cg.CGCancelDisplayConfiguration(config)
        return CgsResult(
            False, "cgs-empty-transaction",
            failed_stage="complete_configuration",
            return_code=rc,
        )
    scope = "permanent" if permanent else "session"
    return CgsResult(
        True, "cgs-empty-transaction",
        message=f"completed ({scope})",
    )


def _load_skylight() -> Any | None:
    paths = [
        "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
        ctypes.util.find_library("SkyLight") or "",
    ]
    for path in paths:
        if not path:
            continue
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def sls_detect_displays(
    *,
    empty_transaction: bool = False,
    permanent: bool = False,
) -> CgsResult:
    """Call SkyLight's private SLSDetectDisplays soft reprobe."""
    if empty_transaction:
        txn = cgs_empty_transaction(permanent=permanent)
        if not txn.success:
            return txn

    sl = _load_skylight()
    if sl is None:
        return CgsResult(
            False, "sls-detect",
            failed_stage="load_skylight",
            message="SkyLight framework not found",
        )
    if not hasattr(sl, "SLSDetectDisplays"):
        return CgsResult(
            False, "sls-detect",
            failed_stage="missing_symbol",
            message="SLSDetectDisplays not found",
        )

    try:
        sl.SLSDetectDisplays.argtypes = []
        sl.SLSDetectDisplays.restype = ctypes.c_int
        rc = int(sl.SLSDetectDisplays())
    except Exception as exc:
        return CgsResult(
            False, "sls-detect",
            failed_stage="call",
            message=str(exc),
        )
    if rc != 0:
        return CgsResult(
            False, "sls-detect",
            failed_stage="detect",
            return_code=rc,
            message="SLSDetectDisplays returned a non-zero code",
        )
    detail = "after empty transaction" if empty_transaction else "soft reprobe"
    return CgsResult(True, "sls-detect", message=detail)


def iokit_request_probe(
    class_names: list[str],
    *,
    probe_option: int = 0,
) -> CgsResult:
    """Ask matching IOKit display services to reprobe themselves."""
    iokit_path = ctypes.util.find_library("IOKit")
    if not iokit_path:
        return CgsResult(
            False, "iokit-probe",
            failed_stage="find_iokit",
            message="IOKit framework not found",
        )
    try:
        iokit = ctypes.CDLL(iokit_path)
    except OSError as exc:
        return CgsResult(
            False, "iokit-probe",
            failed_stage="load_iokit",
            message=str(exc),
        )

    io_object_t = ctypes.c_uint32
    io_iterator_t = ctypes.c_uint32
    io_service_t = ctypes.c_uint32
    kern_return_t = ctypes.c_int
    mach_port_t = ctypes.c_uint32

    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceGetMatchingServices.argtypes = [
        mach_port_t, ctypes.c_void_p, ctypes.POINTER(io_iterator_t),
    ]
    iokit.IOServiceGetMatchingServices.restype = kern_return_t
    iokit.IOIteratorNext.argtypes = [io_iterator_t]
    iokit.IOIteratorNext.restype = io_service_t
    iokit.IOServiceRequestProbe.argtypes = [io_service_t, ctypes.c_uint32]
    iokit.IOServiceRequestProbe.restype = kern_return_t
    iokit.IOObjectRelease.argtypes = [io_object_t]
    iokit.IOObjectRelease.restype = kern_return_t

    probed = 0
    failures: list[str] = []
    kIOMainPortDefault = 0
    for class_name in class_names:
        matching = iokit.IOServiceMatching(class_name.encode("utf-8"))
        if not matching:
            failures.append(f"{class_name}: no matching dictionary")
            continue

        iterator = io_iterator_t(0)
        rc = int(iokit.IOServiceGetMatchingServices(
            kIOMainPortDefault, matching, ctypes.byref(iterator),
        ))
        if rc != 0:
            failures.append(f"{class_name}: match rc=0x{rc:x}")
            continue

        try:
            while True:
                service = int(iokit.IOIteratorNext(iterator))
                if service == 0:
                    break
                try:
                    rc = int(iokit.IOServiceRequestProbe(
                        io_service_t(service), ctypes.c_uint32(probe_option),
                    ))
                    if rc == 0:
                        probed += 1
                    else:
                        failures.append(f"{class_name}:{service}: probe rc=0x{rc:x}")
                finally:
                    iokit.IOObjectRelease(io_object_t(service))
        finally:
            iokit.IOObjectRelease(io_object_t(iterator.value))

    message = f"probed {probed} service(s)"
    if failures:
        message += "; " + "; ".join(failures[:4])
    return CgsResult(
        probed > 0,
        "iokit-probe",
        failed_stage=None if probed > 0 else "no_services_probed",
        message=message,
    )


# ---------------------------------------------------------------------------
# CoreDisplay and IOKit read-only probing
# ---------------------------------------------------------------------------


@dataclass
class DisplayAttributes:
    dispext: str = ""
    product_name: str = ""
    manufacturer_id: str = ""
    serial: str = ""
    year: int = 0
    week: int = 0


@dataclass
class UsbDevice:
    name: str
    product: str = ""
    vendor: str = ""
    vendor_id: int | None = None
    product_id: int | None = None
    location_id: int | None = None
    registry_id: int | None = None
    current_ma: int | None = None
    power_state: str = ""
    billboard_mode: str = ""
    path_hint: str = ""
    candidate_reason: str = ""


@dataclass
class ThunderboltEntry:
    name: str
    entry_class: str
    vendor: str = ""
    model: str = ""
    route: str = ""
    link_rate: str = ""
    link_width: str = ""


def _load_ioreg_plist(class_name: str, *, depth: int = 2) -> list[dict[str, Any]]:
    result = run_command(
        f"ioreg {class_name}",
        ["ioreg", "-r", "-c", class_name, "-d", str(depth), "-a"],
        timeout=20,
    )
    if not result.ok or not result.stdout:
        return []
    try:
        raw = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
        parsed = plistlib.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _walk_entries(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result = [entry]
    for child in entry.get("IORegistryEntryChildren", []) or []:
        if isinstance(child, dict):
            result.extend(_walk_entries(child))
    return result


def _walk_roots(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for root in roots:
        result.extend(_walk_entries(root))
    return result


def query_display_attributes() -> list[DisplayAttributes]:
    attrs: list[DisplayAttributes] = []
    for entry in _load_ioreg_plist("AppleCLCD2", depth=1):
        name = str(entry.get("IONameMatched", ""))
        dispext = name.split(",", 1)[0] if "dispext" in name else ""
        prod = entry.get("DisplayAttributes", {}).get("ProductAttributes", {})
        attrs.append(
            DisplayAttributes(
                dispext=dispext,
                product_name=str(prod.get("ProductName", "")),
                manufacturer_id=str(prod.get("ManufacturerID", "")),
                serial=str(prod.get("AlphanumericSerialNumber", "")),
                year=int(prod.get("YearOfManufacture", 0) or 0),
                week=int(prod.get("WeekOfManufacture", 0) or 0),
            )
        )
    return attrs


def query_usb_devices() -> list[UsbDevice]:
    roots = _load_ioreg_plist("IOUSBHostDevice", depth=3)
    devices: list[UsbDevice] = []
    for root in roots:
        if root.get("IOObjectClass") != "IOUSBHostDevice":
            continue
        children = _walk_entries(root)
        child_classes = {
            str(child.get("IOObjectClass", "")) for child in children if child is not root
        }
        billboard = next(
            (
                child
                for child in children
                if "Billboard" in str(child.get("IOObjectClass", ""))
                or "Billboard" in str(child.get("IORegistryEntryName", ""))
            ),
            {},
        )
        pm = root.get("IOPowerManagement", {}) or {}
        reasons: list[str] = []
        fields = " ".join(
            str(root.get(k, ""))
            for k in (
                "IORegistryEntryName",
                "USB Product Name",
                "USB Vendor Name",
                "USB Serial Number",
            )
        ).lower()
        for token in (
            "display", "hdmi", "dp", "adapter", "billboard", "synaptics",
            "vmm", "parade", "microsoft", "surface", "caldigit",
        ):
            if token in fields:
                reasons.append(token)
        if "AppleUSBHostBillboardDevice" in child_classes or billboard:
            reasons.append("billboard")
        devices.append(
            UsbDevice(
                name=str(root.get("IORegistryEntryName", "")),
                product=str(root.get("USB Product Name", "")),
                vendor=str(root.get("USB Vendor Name", "")),
                vendor_id=_maybe_int(root.get("idVendor")),
                product_id=_maybe_int(root.get("idProduct")),
                location_id=_maybe_int(root.get("locationID")),
                registry_id=_maybe_int(root.get("IORegistryEntryID")),
                current_ma=_maybe_int(root.get("kUSBCurrentConfiguration")),
                power_state=_power_summary(pm),
                billboard_mode=str(billboard.get("UsbBillboardCurrentMode", "")),
                path_hint=", ".join(sorted(c for c in child_classes if c)),
                candidate_reason=", ".join(sorted(set(reasons))),
            )
        )
    return devices


def _maybe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except ValueError:
        return None


def _power_summary(pm: dict[str, Any]) -> str:
    if not isinstance(pm, dict):
        return ""
    fields = []
    for key in ("CurrentPowerState", "DevicePowerState", "DriverPowerState"):
        if key in pm:
            fields.append(f"{key}={pm[key]}")
    return " ".join(fields)


def query_thunderbolt_entries() -> list[ThunderboltEntry]:
    roots = _load_ioreg_plist("IOThunderboltPort", depth=3)
    entries: list[ThunderboltEntry] = []
    for item in _walk_roots(roots):
        cls = str(item.get("IOObjectClass", ""))
        if "Thunderbolt" not in cls:
            continue
        vendor = str(item.get("Device Vendor Name", ""))
        model = str(item.get("Device Model Name", ""))
        if not (vendor or model or cls == "IOThunderboltPort"):
            continue
        entries.append(
            ThunderboltEntry(
                name=str(item.get("IORegistryEntryName", "")),
                entry_class=cls,
                vendor=vendor,
                model=model,
                route=str(item.get("Route String", "")),
                link_rate=str(item.get("Current Link Rate", "")),
                link_width=str(item.get("Current Link Width", "")),
            )
        )
    return entries


def _query_coredisplay_locations() -> dict[int, str]:
    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            return {}
        cg = ctypes.cdll.LoadLibrary(cg_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)
        cd = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreDisplay.framework/CoreDisplay"
        )
    except OSError:
        return {}

    c_uint32 = ctypes.c_uint32
    c_void_p = ctypes.c_void_p
    cg.CGGetActiveDisplayList.argtypes = [
        c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cd.CoreDisplay_DisplayCreateInfoDictionary.argtypes = [c_uint32]
    cd.CoreDisplay_DisplayCreateInfoDictionary.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
    cf.CFDictionaryGetValue.restype = c_void_p
    cf.CFStringGetCStringPtr.argtypes = [c_void_p, c_uint32]
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [c_void_p]
    cf.CFRelease.restype = None

    kCFStringEncodingUTF8 = 0x08000100
    ids = (c_uint32 * 32)()
    count = c_uint32(0)
    if cg.CGGetActiveDisplayList(32, ids, ctypes.byref(count)) != 0:
        return {}

    key = cf.CFStringCreateWithCString(
        None, b"IODisplayLocation", kCFStringEncodingUTF8,
    )
    if not key:
        return {}

    result: dict[int, str] = {}
    try:
        for i in range(count.value):
            did = ids[i]
            info = cd.CoreDisplay_DisplayCreateInfoDictionary(did)
            if not info:
                continue
            try:
                val = cf.CFDictionaryGetValue(info, key)
                if not val:
                    continue
                cstr = cf.CFStringGetCStringPtr(val, kCFStringEncodingUTF8)
                if cstr:
                    location = cstr.decode("utf-8")
                else:
                    buf = ctypes.create_string_buffer(2048)
                    if not cf.CFStringGetCString(
                        val, buf, len(buf), kCFStringEncodingUTF8,
                    ):
                        continue
                    location = buf.value.decode("utf-8")
                result[int(did)] = location
            finally:
                cf.CFRelease(info)
    finally:
        cf.CFRelease(key)
    return result


# ---------------------------------------------------------------------------
# Experimental USB reset helper
# ---------------------------------------------------------------------------


USB_RESET_HELPER_C = r"""
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef kIOMainPortDefault
#define kIOMainPortDefault kIOMasterPortDefault
#endif

static UInt32 number_property(io_service_t service, CFStringRef key) {
    UInt32 value = 0;
    CFTypeRef obj = IORegistryEntryCreateCFProperty(
        service, key, kCFAllocatorDefault, 0);
    if (obj && CFGetTypeID(obj) == CFNumberGetTypeID()) {
        CFNumberGetValue((CFNumberRef)obj, kCFNumberSInt32Type, &value);
    }
    if (obj) CFRelease(obj);
    return value;
}

static void string_property(
    io_service_t service, CFStringRef key, char *buf, size_t len) {
    if (!buf || len == 0) return;
    buf[0] = '\0';
    CFTypeRef obj = IORegistryEntryCreateCFProperty(
        service, key, kCFAllocatorDefault, 0);
    if (obj && CFGetTypeID(obj) == CFStringGetTypeID()) {
        CFStringGetCString((CFStringRef)obj, buf, len, kCFStringEncodingUTF8);
    }
    if (obj) CFRelease(obj);
}

static IOReturn query_device(
    IOCFPlugInInterface **plugin,
    IOUSBDeviceInterface500 ***out_dev) {
    HRESULT hr = (*plugin)->QueryInterface(
        plugin,
        CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID500),
        (LPVOID *)out_dev);
    if (hr == S_OK && *out_dev) return kIOReturnSuccess;

    hr = (*plugin)->QueryInterface(
        plugin,
        CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID300),
        (LPVOID *)out_dev);
    if (hr == S_OK && *out_dev) return kIOReturnSuccess;

    hr = (*plugin)->QueryInterface(
        plugin,
        CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID245),
        (LPVOID *)out_dev);
    if (hr == S_OK && *out_dev) return kIOReturnSuccess;

    return (IOReturn)hr;
}

static int try_class(
    const char *class_name,
    UInt32 target_location,
    const char *action,
    int seize,
    int dry_run) {
    CFMutableDictionaryRef matching = IOServiceMatching(class_name);
    if (!matching) return 2;

    io_iterator_t iter = IO_OBJECT_NULL;
    kern_return_t kr = IOServiceGetMatchingServices(
        kIOMainPortDefault, matching, &iter);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceGetMatchingServices(%s) failed: 0x%08x\n",
                class_name, kr);
        return 2;
    }

    io_service_t service;
    while ((service = IOIteratorNext(iter))) {
        UInt32 location = number_property(service, CFSTR("locationID"));
        if (location != target_location) {
            IOObjectRelease(service);
            continue;
        }

        char name[256];
        char product[256];
        char vendor[256];
        IORegistryEntryGetName(service, name);
        string_property(service, CFSTR("USB Product Name"), product, sizeof(product));
        string_property(service, CFSTR("USB Vendor Name"), vendor, sizeof(vendor));
        printf("matched %s location=0x%08x product='%s' vendor='%s'\n",
               name, location, product, vendor);

        if (dry_run) {
            IOObjectRelease(service);
            IOObjectRelease(iter);
            return 0;
        }

        IOCFPlugInInterface **plugin = NULL;
        SInt32 score = 0;
        kr = IOCreatePlugInInterfaceForService(
            service,
            kIOUSBDeviceUserClientTypeID,
            kIOCFPlugInInterfaceID,
            &plugin,
            &score);
        IOObjectRelease(service);
        if (kr != kIOReturnSuccess || !plugin) {
            IOObjectRelease(iter);
            fprintf(stderr, "IOCreatePlugInInterfaceForService failed: 0x%08x\n", kr);
            return 3;
        }

        IOUSBDeviceInterface500 **dev = NULL;
        kr = query_device(plugin, &dev);
        (*plugin)->Release(plugin);
        if (kr != kIOReturnSuccess || !dev) {
            IOObjectRelease(iter);
            fprintf(stderr, "QueryInterface failed: 0x%08x\n", kr);
            return 4;
        }

        kr = (*dev)->USBDeviceOpen(dev);
        if (kr != kIOReturnSuccess && seize) {
            fprintf(stderr, "USBDeviceOpen failed: 0x%08x; trying seize\n", kr);
            kr = (*dev)->USBDeviceOpenSeize(dev);
        }
        if (kr != kIOReturnSuccess) {
            (*dev)->Release(dev);
            IOObjectRelease(iter);
            fprintf(stderr, "USBDeviceOpen failed: 0x%08x\n", kr);
            return 5;
        }

        if (strcmp(action, "reset") == 0) {
            kr = (*dev)->ResetDevice(dev);
        } else if (strcmp(action, "reenumerate") == 0) {
            kr = (*dev)->USBDeviceReEnumerate(dev, 0);
        } else if (strcmp(action, "open-close") == 0) {
            kr = kIOReturnSuccess;
        } else {
            kr = kIOReturnBadArgument;
        }

        IOReturn close_kr = (*dev)->USBDeviceClose(dev);
        (*dev)->Release(dev);
        IOObjectRelease(iter);
        if (kr != kIOReturnSuccess) {
            fprintf(stderr, "%s failed: 0x%08x\n", action, kr);
            return 6;
        }
        if (close_kr != kIOReturnSuccess) {
            fprintf(stderr, "USBDeviceClose failed: 0x%08x\n", close_kr);
            return 7;
        }
        printf("%s completed for location=0x%08x\n", action, target_location);
        return 0;
    }

    IOObjectRelease(iter);
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <location-id> <action> <seize:0|1> [dry-run:0|1]\n",
                argv[0]);
        return 64;
    }
    UInt32 location = (UInt32)strtoul(argv[1], NULL, 0);
    const char *action = argv[2];
    int seize = atoi(argv[3]);
    int dry_run = argc > 4 ? atoi(argv[4]) : 0;

    int rc = try_class("IOUSBHostDevice", location, action, seize, dry_run);
    if (rc == 1) {
        rc = try_class("IOUSBDevice", location, action, seize, dry_run);
    }
    if (rc == 1) {
        fprintf(stderr, "No USB device found at location=0x%08x\n", location);
    }
    return rc;
}
"""


def _compile_usb_reset_helper(tmpdir: Path) -> tuple[Path | None, str]:
    clang = shutil.which("clang")
    if not clang:
        return None, "clang not found; install Xcode Command Line Tools"

    source = tmpdir / "usb_device_reset_helper.c"
    binary = tmpdir / "usb_device_reset_helper"
    source.write_text(USB_RESET_HELPER_C)
    result = run_command(
        "compile usb reset helper",
        [
            clang,
            str(source),
            "-o",
            str(binary),
            "-framework",
            "IOKit",
            "-framework",
            "CoreFoundation",
        ],
        timeout=20,
    )
    if not result.ok:
        return None, result.summary
    return binary, ""


def run_usb_reset_helper(
    location_id: int,
    *,
    action: str,
    seize: bool,
    dry_run: bool,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="display-port-reset-") as tmp:
        helper, error = _compile_usb_reset_helper(Path(tmp))
        if helper is None:
            return CommandResult(
                "usb reset helper compile",
                [],
                127,
                stderr=error,
            )
        return run_command(
            f"usb {action}",
            [
                str(helper),
                str(location_id),
                action,
                "1" if seize else "0",
                "1" if dry_run else "0",
            ],
            timeout=20,
        )


# ---------------------------------------------------------------------------
# Snapshot and reporting
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    displayplacer: list[Display]
    coregraphics: list[CoreGraphicsDisplay]
    coredisplay_locations: dict[int, str]
    display_attributes: list[DisplayAttributes]
    usb_devices: list[UsbDevice]
    thunderbolt: list[ThunderboltEntry]


def collect_snapshot() -> Snapshot:
    dp_result = run_displayplacer_list()
    displays = parse_displays(dp_result.stdout) if dp_result.ok else []
    return Snapshot(
        displayplacer=displays,
        coregraphics=coregraphics_snapshot(),
        coredisplay_locations=_query_coredisplay_locations(),
        display_attributes=query_display_attributes(),
        usb_devices=query_usb_devices(),
        thunderbolt=query_thunderbolt_entries(),
    )


def print_snapshot(snapshot: Snapshot) -> None:
    print("DisplayPort/HDMI recovery diagnostics")
    print()
    _print_displayplacer(snapshot.displayplacer)
    _print_coregraphics(snapshot.coregraphics)
    _print_coredisplay(snapshot.coredisplay_locations, snapshot.display_attributes)
    _print_usb(snapshot.usb_devices)
    _print_thunderbolt(snapshot.thunderbolt)
    _print_suggestions(snapshot)


def _print_displayplacer(displays: list[Display]) -> None:
    print("displayplacer")
    if not displays:
        print("  no displayplacer displays parsed")
        return
    for d in displays:
        main = " main" if "main display" in d.origin else ""
        print(
            f"  [{d.contextual_id}] {d.type or 'display'}{main} "
            f"enabled={d.enabled or 'unknown'}"
        )
        print(
            f"      serial={d.serial_id or 'unknown'} "
            f"res={d.resolution or 'unknown'} hz={d.hertz or 'unknown'} "
            f"origin={d.origin or 'unknown'}"
        )


def _print_coregraphics(displays: list[CoreGraphicsDisplay]) -> None:
    print()
    print("CoreGraphics / CGS")
    if not displays:
        print("  no CoreGraphics displays parsed")
        return
    for d in displays:
        flags = []
        if d.active:
            flags.append("active")
        if d.online:
            flags.append("online")
        if d.main:
            flags.append("main")
        if d.builtin:
            flags.append("built-in")
        print(
            f"  [{d.display_id}] {', '.join(flags) or 'inactive'} "
            f"serial={d.serial} vendor={_fmt_hex(d.vendor)} "
            f"model={_fmt_hex(d.model)} bounds={d.bounds or 'unknown'}"
        )


def _print_coredisplay(
    locations: dict[int, str],
    attributes: list[DisplayAttributes],
) -> None:
    print()
    print("CoreDisplay / AppleCLCD2")
    if locations:
        for display_id, location in sorted(locations.items()):
            print(f"  [{display_id}] IODisplayLocation={location}")
    else:
        print("  no CoreDisplay IODisplayLocation data")
    if attributes:
        for attr in attributes:
            print(
                f"      {attr.dispext or 'dispext?'} "
                f"{attr.manufacturer_id} {attr.product_name} "
                f"serial={attr.serial or 'unknown'} "
                f"mfg={attr.year}-W{attr.week}"
            )


def _print_usb(devices: list[UsbDevice]) -> None:
    print()
    print("USB / USB-C adapter candidates")
    candidates = [d for d in devices if d.candidate_reason]
    if not candidates:
        print("  no obvious USB display adapter or Billboard device candidates")
        return
    for dev in candidates:
        print(
            f"  location={_fmt_location(dev.location_id)} "
            f"vendor={dev.vendor or 'unknown'} product={dev.product or dev.name}"
        )
        print(
            f"      idVendor={_fmt_hex(dev.vendor_id)} "
            f"idProduct={_fmt_hex(dev.product_id)} "
            f"registry={dev.registry_id or 'unknown'}"
        )
        if dev.billboard_mode:
            print(f"      Billboard current mode: {dev.billboard_mode}")
        if dev.power_state:
            print(f"      power: {dev.power_state}")
        print(f"      candidate reason: {dev.candidate_reason}")


def _print_thunderbolt(entries: list[ThunderboltEntry]) -> None:
    print()
    print("Thunderbolt")
    interesting = [
        item for item in entries
        if item.vendor or item.model or item.entry_class == "IOThunderboltPort"
    ]
    if not interesting:
        print("  no Thunderbolt entries parsed")
        return
    for item in interesting[:16]:
        label = f"{item.vendor} {item.model}".strip() or item.name
        details = []
        if item.route:
            details.append(f"route={item.route}")
        if item.link_rate:
            details.append(f"rate={item.link_rate}")
        if item.link_width:
            details.append(f"width={item.link_width}")
        print(f"  {item.entry_class}: {label}")
        if details:
            print(f"      {' '.join(details)}")


def _print_suggestions(snapshot: Snapshot) -> None:
    print()
    print("Suggested experiment ladder")
    active_ids = [d.contextual_id for d in snapshot.displayplacer if d.contextual_id]
    if active_ids:
        ids = ", ".join(active_ids)
        print(f"  display ids currently visible to displayplacer: {ids}")
    print("  1. ./display-port-recovery-diagnostics.py sls-detect")
    print("  2. ./display-port-recovery-diagnostics.py reapply-mode --id 3")
    print(
        "  3. ./display-port-recovery-diagnostics.py ddc-dpms-cycle "
        "--display 'ASUS PG32UQ' --yes"
    )
    print("  4. ./display-port-recovery-diagnostics.py sleep-displays --yes")
    print(
        "  5. ./display-port-recovery-diagnostics.py mode-cycle --id 3 "
        "--temporary-res 1920x1080 --temporary-hz 60 --yes"
    )
    print("  6. ./display-port-recovery-diagnostics.py iokit-probe")
    candidates = _rank_usb_recovery_candidates(snapshot.usb_devices)
    if candidates:
        print("  7. USB reset candidates (match the physical adapter first):")
        for usb in candidates[:4]:
            if usb.location_id is None:
                continue
            label = usb.product or usb.name or "USB device"
            print(
                "     ./display-port-recovery-diagnostics.py usb-reset "
                f"--location {_fmt_location(usb.location_id).split()[0]} "
                f"--dry-run    # {label}"
            )
        print("     then repeat the matching command without --dry-run and with --yes")
    print("  Optional: displaypolicyd-restart --yes")
    print("  Last resort: windowserver-restart --yes (logs out the GUI session)")


def _rank_usb_recovery_candidates(devices: list[UsbDevice]) -> list[UsbDevice]:
    scored: list[tuple[int, UsbDevice]] = []
    for dev in devices:
        if dev.location_id is None or not dev.candidate_reason:
            continue
        text = " ".join((dev.name, dev.product, dev.vendor, dev.candidate_reason)).lower()
        score = 0
        for token in ("hdmi", "display", "adapter", "dp", "vmm", "synaptics"):
            if token in text:
                score += 4
        for token in ("microsoft", "surface", "billboard"):
            if token in text:
                score += 2
        if "caldigit" in text and "hub" in text:
            score -= 5
        scored.append((score, dev))
    return [dev for _score, dev in sorted(scored, key=lambda item: item[0], reverse=True)]


def snapshot_as_json(snapshot: Snapshot) -> str:
    data = asdict(snapshot)
    data["coredisplay_locations"] = {
        str(k): v for k, v in snapshot.coredisplay_locations.items()
    }
    return json.dumps(data, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Experimental actions
# ---------------------------------------------------------------------------


def _current_display_or_error(display_id: int) -> Display | None:
    result = run_displayplacer_list()
    if not result.ok:
        print(f"Error: {result.summary}", file=sys.stderr)
        return None
    displays = parse_displays(result.stdout)
    display = _display_by_id(displays, display_id)
    if display is None:
        print(
            f"Error: displayplacer does not currently list display id {display_id}.",
            file=sys.stderr,
        )
        return None
    return display


def cmd_snapshot(args: argparse.Namespace) -> int:
    snapshot = collect_snapshot()
    if args.json:
        print(snapshot_as_json(snapshot))
    else:
        print_snapshot(snapshot)
    return 0


def cmd_sls_detect(args: argparse.Namespace) -> int:
    result = sls_detect_displays(
        empty_transaction=args.empty_transaction,
        permanent=args.permanent,
    )
    print(result.summary)
    return 0 if result.success else 1


def cmd_iokit_probe(args: argparse.Namespace) -> int:
    result = iokit_request_probe(args.class_name, probe_option=_parse_uint(args.option))
    print(result.summary)
    return 0 if result.success else 1


def cmd_cgs_enable(args: argparse.Namespace) -> int:
    result = cgs_set_enabled([args.id], True, permanent=args.permanent)
    print(result.summary)
    return 0 if result.success else 1


def cmd_cgs_toggle(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "CGS disable/enable toggle"):
        return 2
    first = cgs_set_enabled([args.id], False, permanent=args.permanent)
    print(first.summary)
    if not first.success:
        return 1
    time.sleep(args.seconds)
    second = cgs_set_enabled([args.id], True, permanent=args.permanent)
    print(second.summary)
    return 0 if second.success else 1


def cmd_displayplacer_enable(args: argparse.Namespace) -> int:
    result = _run_id_enable("displayplacer enable", args.id, True)
    print(result.summary)
    return 0 if result.ok else 1


def cmd_displayplacer_toggle(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "displayplacer disable/enable toggle"):
        return 2
    first = _run_id_enable("displayplacer disable", args.id, False)
    print(first.summary)
    if not first.ok:
        return 1
    time.sleep(args.seconds)
    second = _run_id_enable("displayplacer enable", args.id, True)
    print(second.summary)
    return 0 if second.ok else 1


def cmd_reapply_mode(args: argparse.Namespace) -> int:
    display = _current_display_or_error(args.id)
    if display is None:
        return 1
    arg = _displayplacer_arg(display, enabled=True)
    print(f"displayplacer \"{arg}\"")
    if args.dry_run:
        return 0
    result = _run_displayplacer_arg("displayplacer reapply mode", arg)
    print(result.summary)
    return 0 if result.ok else 1


def cmd_mode_cycle(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "display mode cycle"):
        return 2
    display = _current_display_or_error(args.id)
    if display is None:
        return 1
    temporary = _displayplacer_arg(
        display,
        enabled=True,
        resolution=args.temporary_res,
        hertz=args.temporary_hz,
    )
    restore = _displayplacer_arg(display, enabled=True)
    print(f"temporary: displayplacer \"{temporary}\"")
    first = _run_displayplacer_arg("displayplacer temporary mode", temporary)
    print(first.summary)
    if not first.ok:
        return 1
    time.sleep(args.seconds)
    print(f"restore: displayplacer \"{restore}\"")
    second = _run_displayplacer_arg("displayplacer restore mode", restore)
    print(second.summary)
    return 0 if second.ok else 1


def cmd_sleep_displays(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "display sleep + wake"):
        return 2
    result = run_command("pmset displaysleepnow", ["pmset", "displaysleepnow"])
    print(result.summary)
    if not result.ok or args.no_wake:
        return 0 if result.ok else 1
    time.sleep(args.seconds)
    wake = run_command("caffeinate wake", ["caffeinate", "-u", "-t", "1"])
    print(wake.summary)
    return 0 if result.ok and wake.ok else 1


def cmd_displaypolicyd_restart(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "displaypolicyd restart"):
        return 2
    result = run_command(
        "restart displaypolicyd",
        ["sudo", "killall", "-9", "displaypolicyd"],
        capture=True,
        timeout=30,
    )
    print(result.summary)
    return 0 if result.ok else 1


def cmd_ddc_dpms_cycle(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "DDC/CI DPMS cycle"):
        return 2
    if not shutil.which("m1ddc"):
        print("Error: m1ddc not found in PATH.", file=sys.stderr)
        print("Install m1ddc before trying DDC/CI DPMS recovery.", file=sys.stderr)
        return 127
    off = run_command(
        "m1ddc DPMS off",
        ["m1ddc", "display", args.display, "set", "0xD6", str(args.off_value)],
        timeout=20,
    )
    print(off.summary)
    if not off.ok:
        return 1
    time.sleep(args.seconds)
    on = run_command(
        "m1ddc DPMS on",
        ["m1ddc", "display", args.display, "set", "0xD6", str(args.on_value)],
        timeout=20,
    )
    print(on.summary)
    if on.ok and args.detect_after:
        detect = sls_detect_displays()
        print(detect.summary)
        return 0 if detect.success else 1
    return 0 if on.ok else 1


def cmd_windowserver_restart(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "WindowServer restart"):
        return 2
    print("This logs out the active GUI session.")
    result = run_command(
        "restart WindowServer",
        ["sudo", "killall", "-HUP", "WindowServer"],
        capture=True,
        timeout=30,
    )
    print(result.summary)
    return 0 if result.ok else 1


def cmd_usb_reset(args: argparse.Namespace) -> int:
    location = _parse_uint(args.location)
    if not args.dry_run and not _require_confirmation(args, "USB device reset"):
        return 2
    result = run_usb_reset_helper(
        location,
        action=args.action,
        seize=args.seize,
        dry_run=args.dry_run,
    )
    print(result.summary)
    detail = _compact_process_output(result.stdout or result.stderr, limit=10)
    if detail:
        print(detail)
    return 0 if result.ok else 1


def cmd_recover(args: argparse.Namespace) -> int:
    if not _require_confirmation(args, "multi-step recovery sequence"):
        return 2

    steps: list[tuple[str, Any]] = [
        ("sls-detect", lambda: sls_detect_displays()),
        (
            "iokit-probe",
            lambda: iokit_request_probe(["AppleCLCD2", "AppleDisplay"]),
        ),
        ("displayplacer-enable", lambda: _run_id_enable("displayplacer enable", args.id, True)),
        ("cgs-enable", lambda: cgs_set_enabled([args.id], True, permanent=False)),
        ("reapply-mode", lambda: _recover_reapply(args.id)),
    ]
    if args.ddc_dpms:
        steps.append(
            (
                "ddc-dpms-cycle",
                lambda: _recover_ddc_dpms(args.ddc_display, args.seconds),
            )
        )
    if args.sleep_wake:
        steps.append(("sleep-displays", lambda: _recover_sleep_wake(args.seconds)))
    steps.append(
        (
            "mode-cycle",
            lambda: _recover_mode_cycle(
                args.id, args.temporary_res, args.temporary_hz, args.seconds,
            ),
        )
    )
    if args.displaypolicyd:
        steps.append(("displaypolicyd-restart", _recover_displaypolicyd_restart))
    if args.usb_location:
        steps.append(
            (
                "usb-reset",
                lambda: run_usb_reset_helper(
                    _parse_uint(args.usb_location),
                    action=args.usb_action,
                    seize=args.seize,
                    dry_run=False,
                ),
            )
        )

    overall_ok = True
    for name, action in steps:
        _log(f"Running {name}...")
        result = action()
        if isinstance(result, CgsResult):
            print(result.summary)
            ok = result.success
        else:
            print(result.summary)
            ok = result.ok
        overall_ok = overall_ok and ok
        time.sleep(args.step_delay)

    _log("Collecting final snapshot...")
    print_snapshot(collect_snapshot())
    return 0 if overall_ok else 1


def _recover_reapply(display_id: int) -> CommandResult:
    display = _current_display_or_error(display_id)
    if display is None:
        return CommandResult("displayplacer reapply mode", [], 1, stderr="display not found")
    return _run_displayplacer_arg(
        "displayplacer reapply mode",
        _displayplacer_arg(display, enabled=True),
    )


def _recover_mode_cycle(
    display_id: int,
    temporary_res: str,
    temporary_hz: int,
    seconds: float,
) -> CommandResult:
    display = _current_display_or_error(display_id)
    if display is None:
        return CommandResult("displayplacer mode cycle", [], 1, stderr="display not found")
    temporary = _displayplacer_arg(
        display,
        enabled=True,
        resolution=temporary_res,
        hertz=temporary_hz,
    )
    first = _run_displayplacer_arg("displayplacer temporary mode", temporary)
    if not first.ok:
        return first
    time.sleep(seconds)
    return _run_displayplacer_arg(
        "displayplacer restore mode",
        _displayplacer_arg(display, enabled=True),
    )


def _recover_ddc_dpms(display: str, seconds: float) -> CommandResult:
    if not shutil.which("m1ddc"):
        return CommandResult("m1ddc DPMS cycle", [], 127, stderr="m1ddc not found")
    off = run_command(
        "m1ddc DPMS off",
        ["m1ddc", "display", display, "set", "0xD6", "4"],
        timeout=20,
    )
    if not off.ok:
        return off
    time.sleep(seconds)
    on = run_command(
        "m1ddc DPMS on",
        ["m1ddc", "display", display, "set", "0xD6", "1"],
        timeout=20,
    )
    if not on.ok:
        return on
    detect = sls_detect_displays()
    return CommandResult(
        "m1ddc DPMS cycle",
        [],
        0 if detect.success else 1,
        stdout=detect.summary,
    )


def _recover_sleep_wake(seconds: float) -> CommandResult:
    sleep = run_command("pmset displaysleepnow", ["pmset", "displaysleepnow"])
    if not sleep.ok:
        return sleep
    time.sleep(seconds)
    return run_command("caffeinate wake", ["caffeinate", "-u", "-t", "1"])


def _recover_displaypolicyd_restart() -> CommandResult:
    return run_command(
        "restart displaypolicyd",
        ["sudo", "killall", "-9", "displaypolicyd"],
        capture=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated diagnostics and experimental recovery for macOS "
            "USB-C/HDMI/DisplayPort wake failures."
        )
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("snapshot", help="read-only display, USB, and Thunderbolt probe")
    p.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("sls-detect", help="SkyLight SLSDetectDisplays soft reprobe")
    p.add_argument(
        "--empty-transaction",
        action="store_true",
        help="commit an empty CG display transaction before detecting",
    )
    p.add_argument(
        "--permanent",
        action="store_true",
        help="make the optional empty transaction permanent",
    )
    p.set_defaults(func=cmd_sls_detect)

    p = sub.add_parser("iokit-probe", help="IOServiceRequestProbe display services")
    p.add_argument(
        "--class-name",
        action="append",
        default=["AppleCLCD2", "AppleDisplay"],
        help="IOService class to probe; repeatable",
    )
    p.add_argument(
        "--option",
        default="0",
        help="probe option, decimal or hex; default is kIOFBUserRequestProbe/0",
    )
    p.set_defaults(func=cmd_iokit_probe)

    p = sub.add_parser("cgs-enable", help="private CGS enabled:true for one display")
    p.add_argument("--id", type=int, required=True, help="contextual CG/displayplacer id")
    p.add_argument("--permanent", action="store_true", help="commit permanently")
    p.set_defaults(func=cmd_cgs_enable)

    p = sub.add_parser("cgs-toggle", help="private CGS disable then enable")
    p.add_argument("--id", type=int, required=True, help="contextual CG/displayplacer id")
    p.add_argument("--seconds", type=float, default=2.0, help="delay while disabled")
    p.add_argument("--permanent", action="store_true", help="commit permanently")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_cgs_toggle)

    p = sub.add_parser("displayplacer-enable", help="displayplacer id:<id> enabled:true")
    p.add_argument("--id", type=int, required=True, help="displayplacer contextual id")
    p.set_defaults(func=cmd_displayplacer_enable)

    p = sub.add_parser("displayplacer-toggle", help="displayplacer disable then enable")
    p.add_argument("--id", type=int, required=True, help="displayplacer contextual id")
    p.add_argument("--seconds", type=float, default=2.0, help="delay while disabled")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_displayplacer_toggle)

    p = sub.add_parser("reapply-mode", help="reapply the current displayplacer mode")
    p.add_argument("--id", type=int, required=True, help="displayplacer contextual id")
    p.add_argument("--dry-run", action="store_true", help="print command only")
    p.set_defaults(func=cmd_reapply_mode)

    p = sub.add_parser("mode-cycle", help="switch to a safe mode, then restore")
    p.add_argument("--id", type=int, required=True, help="displayplacer contextual id")
    p.add_argument("--temporary-res", default="1920x1080")
    p.add_argument("--temporary-hz", type=int, default=60)
    p.add_argument("--seconds", type=float, default=3.0, help="delay before restore")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_mode_cycle)

    p = sub.add_parser("sleep-displays", help="sleep displays, then caffeinate-wake")
    p.add_argument("--seconds", type=float, default=2.0, help="delay before wake")
    p.add_argument("--no-wake", action="store_true", help="skip caffeinate wake")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_sleep_displays)

    p = sub.add_parser("displaypolicyd-restart", help="restart displaypolicyd")
    p.add_argument("--yes", action="store_true", help="confirm daemon restart")
    p.set_defaults(func=cmd_displaypolicyd_restart)

    p = sub.add_parser("ddc-dpms-cycle", help="m1ddc VESA DPMS off/on cycle")
    p.add_argument("--display", default="ASUS PG32UQ", help="m1ddc display selector")
    p.add_argument("--seconds", type=float, default=2.0, help="delay while DPMS off")
    p.add_argument("--off-value", type=int, default=4, help="VCP 0xD6 off value")
    p.add_argument("--on-value", type=int, default=1, help="VCP 0xD6 on value")
    p.add_argument(
        "--no-detect-after",
        dest="detect_after",
        action="store_false",
        help="skip SLSDetectDisplays after DPMS on",
    )
    p.set_defaults(func=cmd_ddc_dpms_cycle, detect_after=True)
    p.add_argument("--yes", action="store_true", help="confirm DDC/CI write")

    p = sub.add_parser("windowserver-restart", help="restart WindowServer via killall")
    p.add_argument("--yes", action="store_true", help="confirm GUI logout action")
    p.set_defaults(func=cmd_windowserver_restart)

    p = sub.add_parser("usb-reset", help="experimental IOUSBLib reset by locationID")
    p.add_argument(
        "--location",
        required=True,
        help="USB locationID from snapshot, decimal or hex",
    )
    p.add_argument(
        "--action",
        choices=("reset", "reenumerate", "open-close"),
        default="reset",
    )
    p.add_argument("--seize", action="store_true", help="try USBDeviceOpenSeize")
    p.add_argument("--dry-run", action="store_true", help="compile and match only")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_usb_reset)

    p = sub.add_parser("recover", help="run a guarded recovery sequence")
    p.add_argument("--id", type=int, required=True, help="displayplacer contextual id")
    p.add_argument("--temporary-res", default="1920x1080")
    p.add_argument("--temporary-hz", type=int, default=60)
    p.add_argument("--seconds", type=float, default=3.0, help="mode cycle delay")
    p.add_argument("--step-delay", type=float, default=2.0, help="delay after each step")
    p.add_argument(
        "--ddc-dpms",
        action="store_true",
        help="include m1ddc DPMS off/on cycle",
    )
    p.add_argument("--ddc-display", default="ASUS PG32UQ", help="m1ddc display")
    p.add_argument(
        "--sleep-wake",
        action="store_true",
        help="include pmset displaysleepnow + caffeinate wake",
    )
    p.add_argument(
        "--displaypolicyd",
        action="store_true",
        help="include sudo killall -9 displaypolicyd",
    )
    p.add_argument("--usb-location", help="optional USB locationID to reset")
    p.add_argument(
        "--usb-action",
        choices=("reset", "reenumerate", "open-close"),
        default="reset",
    )
    p.add_argument("--seize", action="store_true", help="try USBDeviceOpenSeize")
    p.add_argument("--yes", action="store_true", help="confirm experimental action")
    p.set_defaults(func=cmd_recover)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["snapshot"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

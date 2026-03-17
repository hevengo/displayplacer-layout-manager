#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml", "rumps"]
# ///

"""Manage display arrangements using displayplacer."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML


def _log(msg: str) -> None:
    """Timestamped log line (useful in daemon mode where stdout goes to a file)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


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
    def is_main(self) -> bool:
        return "main display" in self.origin


@dataclass
class KnownScreen:
    """A known monitor model.  None fields → use whatever the display reports."""

    label: str
    serial_id: str
    alpha_serial: str | None = None
    brand: str | None = None
    production_year: int | None = None
    production_week: int | None = None
    product_name: str | None = None
    resolution: str | None = None
    hertz: int | None = None
    fallback_hertz: int | None = None
    color_depth: int | None = None
    scaling: str | None = None
    enabled: str | None = None


@dataclass
class MatchedDisplay:
    display: Display
    key: str
    known: KnownScreen
    instance: int  # 1-based; >1 when multiple screens share a serial


@dataclass
class Layout:
    name: str
    positions: list[str]
    main: str
    match: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    preferred: bool = False


@dataclass
class Options:
    enable_menu_bar: bool = False


@dataclass
class HWInfo:
    """Hardware info extracted from ioreg via CoreDisplay identification chain."""

    alpha_serial: str = ""
    product_name: str = ""
    manufacturer_id: str = ""
    year_of_manufacture: int = 0
    week_of_manufacture: int = 0

    @property
    def brand(self) -> str:
        return _PNP_BRANDS.get(self.manufacturer_id, self.manufacturer_id)

    @property
    def display_name(self) -> str:
        brand = self.brand
        name = self.product_name
        if brand and name and not name.upper().startswith(brand.upper()):
            return f"{brand} {name}"
        return name or brand or ""


_PNP_BRANDS: dict[str, str] = {
    "AAC": "AcerView",
    "ACR": "Acer",
    "AUS": "ASUS",
    "BNQ": "BenQ",
    "DEL": "Dell",
    "GSM": "LG",
    "HPN": "HP",
    "HWP": "HP",
    "LEN": "Lenovo",
    "PHL": "Philips",
    "SAM": "Samsung",
    "VSC": "ViewSonic",
}


# ---------------------------------------------------------------------------
# Config loading (config.yml)
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yml"


class ConfigError(SystemExit):
    """Raised for config validation failures (exits with code 1)."""

    def __init__(self, msg: str) -> None:
        super().__init__(f"Config error: {msg}")


def load_config(
    path: Path,
) -> tuple[dict[str, KnownScreen], dict[tuple[str, ...], list[Layout]], Options]:
    """Load displays and layouts from a YAML config file.

    Returns (known_screens, device_set_layouts, options).
    """
    ry = YAML()
    with open(path) as f:
        raw = ry.load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a valid YAML mapping")

    # --- options ---
    raw_options = raw.get("options") or {}
    options = Options(
        enable_menu_bar=bool(raw_options.get("enable-menu-bar", False)),
    )

    # --- displays ---
    raw_displays = raw.get("displays", [])
    if not isinstance(raw_displays, list):
        raise ConfigError(f"'displays' in {path} must be a list")

    known_screens: dict[str, KnownScreen] = {}
    for i, entry in enumerate(raw_displays):
        did = entry.get("id")
        if not did:
            raise ConfigError(f"display #{i + 1} in {path} is missing 'id'")
        did = str(did)
        if did in known_screens:
            raise ConfigError(f"duplicate display id '{did}' in {path}")

        label = str(entry.get("label", did))
        match = entry.get("match", {})
        serial = str(match.get("serial", ""))
        if not serial:
            raise ConfigError(f"display '{did}' is missing 'match.serial'")

        edid_serial = match.get("edid_serial")
        if edid_serial is not None:
            edid_serial = str(edid_serial)

        brand = match.get("brand")
        if brand is not None:
            brand = str(brand)
        production_year = match.get("production_year")
        if production_year is not None:
            production_year = int(production_year)
        production_week = match.get("production_week")
        if production_week is not None:
            production_week = int(production_week)
        product_name = match.get("product_name")
        if product_name is not None:
            product_name = str(product_name)

        settings = entry.get("settings") or {}
        resolution = settings.get("resolution")
        hertz = settings.get("hertz")
        fallback_hertz = settings.get("fallback_hertz")
        color_depth = settings.get("color_depth")

        scaling_raw = settings.get("scaling")
        scaling = None
        if scaling_raw is not None:
            scaling = "on" if scaling_raw else "off"

        enabled_raw = settings.get("enabled")
        enabled = None
        if enabled_raw is not None:
            enabled = "true" if enabled_raw else "false"

        known_screens[did] = KnownScreen(
            label=label,
            serial_id=serial,
            alpha_serial=edid_serial,
            brand=brand,
            production_year=production_year,
            production_week=production_week,
            product_name=product_name,
            resolution=resolution,
            hertz=hertz,
            fallback_hertz=fallback_hertz,
            color_depth=color_depth,
            scaling=scaling,
            enabled=enabled,
        )

    # --- layouts ---
    raw_layouts = raw.get("layouts", [])
    if not isinstance(raw_layouts, list):
        raise ConfigError(f"'layouts' in {path} must be a list")

    device_set_layouts: dict[tuple[str, ...], list[Layout]] = {}
    for i, entry in enumerate(raw_layouts):
        name = entry.get("name", f"Layout #{i + 1}")
        positions = [str(p) for p in entry.get("positions", [])]
        disabled = [str(d) for d in entry.get("disabled", [])]
        raw_enabled = entry.get("enabled")
        raw_match = entry.get("match")
        main = entry.get("main")
        is_preferred = bool(entry.get("preferred", False))

        if not positions:
            raise ConfigError(
                f"layout '{name}' has no 'positions' — "
                "at least one display must be enabled"
            )

        enabled = (
            [str(e) for e in raw_enabled] if raw_enabled is not None
            else list(positions)
        )
        if set(enabled) != set(positions):
            raise ConfigError(
                f"layout '{name}': 'enabled' and 'positions' must contain "
                "the same displays"
            )

        match_ids = (
            sorted(str(m) for m in raw_match) if raw_match is not None
            else []
        )

        for pos in positions:
            if pos not in known_screens:
                raise ConfigError(
                    f"layout '{name}' references unknown display '{pos}'"
                )
        for dis in disabled:
            if dis not in known_screens:
                raise ConfigError(
                    f"layout '{name}' disables unknown display '{dis}'"
                )
        for mid in match_ids:
            if mid not in known_screens:
                raise ConfigError(
                    f"layout '{name}' match references unknown display '{mid}'"
                )
        overlap = set(enabled) & set(disabled)
        if overlap:
            raise ConfigError(
                f"layout '{name}': display(s) {', '.join(sorted(overlap))} "
                "appear in both 'enabled' and 'disabled'"
            )
        if main and main not in enabled:
            raise ConfigError(
                f"layout '{name}': main '{main}' is not in enabled displays"
            )
        if not main:
            main = positions[0]

        sig = tuple(sorted(match_ids))
        device_set_layouts.setdefault(sig, []).append(
            Layout(
                name=name, positions=positions, main=main,
                match=match_ids, enabled=enabled,
                disabled=disabled, preferred=is_preferred,
            )
        )

    for sig, layouts in device_set_layouts.items():
        preferred = [lay for lay in layouts if lay.preferred]
        if len(preferred) > 1:
            names = ", ".join(f"'{p.name}'" for p in preferred)
            raise ConfigError(
                f"multiple layouts marked as preferred for the same "
                f"match set: {names}"
            )

    return known_screens, device_set_layouts, options


# ---------------------------------------------------------------------------
# Config writing (comment-preserving round-trip via ruamel.yaml)
# ---------------------------------------------------------------------------


def _load_raw_config(path: Path):
    """Load config.yml as a ruamel CommentedMap, preserving comments."""
    ry = YAML()
    with open(path) as f:
        data = ry.load(f)
    return data, ry


def _save_raw_config(path: Path, data, ry) -> None:
    """Write the CommentedMap back to config.yml, preserving comments."""
    with open(path, "w") as f:
        ry.dump(data, f)


def _add_display(
    data, *, did: str, label: str, serial: str,
    edid_serial: str | None = None,
    brand: str | None = None,
    production_year: int | None = None,
    production_week: int | None = None,
    product_name: str | None = None,
    settings: dict | None = None,
) -> None:
    """Append a new display entry to the raw config data."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if "displays" not in data or data["displays"] is None:
        data["displays"] = CommentedSeq()
    entry = CommentedMap()
    entry["id"] = did
    entry["label"] = label
    match = CommentedMap()
    match["serial"] = serial
    if edid_serial:
        match["edid_serial"] = edid_serial
    if brand:
        match["brand"] = brand
    if production_year:
        match["production_year"] = production_year
    if production_week:
        match["production_week"] = production_week
    if product_name:
        match["product_name"] = product_name
    entry["match"] = match
    if settings:
        s = CommentedMap()
        for k, v in settings.items():
            s[k] = v
        entry["settings"] = s
    data["displays"].append(entry)


def _update_display(data, did: str, **changes) -> bool:
    """Update fields on an existing display entry. Returns True if found."""
    for entry in data.get("displays", []):
        if str(entry.get("id")) == did:
            for k, v in changes.items():
                if k in ("resolution", "hertz", "fallback_hertz",
                         "color_depth", "scaling", "enabled"):
                    if "settings" not in entry or entry["settings"] is None:
                        from ruamel.yaml.comments import CommentedMap
                        entry["settings"] = CommentedMap()
                    entry["settings"][k] = v
                elif k == "label":
                    entry["label"] = v
            return True
    return False


def _remove_display(data, did: str) -> bool:
    """Remove a display and all references to it from layouts. Returns True if found."""
    displays = data.get("displays", [])
    found = False
    for i, entry in enumerate(displays):
        if str(entry.get("id")) == did:
            del displays[i]
            found = True
            break
    if found:
        for lay in data.get("layouts", []):
            for key in ("match", "enabled", "positions", "disabled"):
                lst = lay.get(key, [])
                while did in lst:
                    lst.remove(did)
    return found


def _add_layout(
    data, *, name: str, positions: list[str], main: str,
    match_ids: list[str] | None = None,
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
    is_preferred: bool = False,
) -> None:
    """Append a new layout entry to the raw config data."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if "layouts" not in data or data["layouts"] is None:
        data["layouts"] = CommentedSeq()
    entry = CommentedMap()
    entry["name"] = name
    if match_ids:
        m_seq = CommentedSeq(match_ids)
        m_seq.fa.set_flow_style()
        entry["match"] = m_seq
    if is_preferred:
        entry["preferred"] = True
    if enabled:
        e_seq = CommentedSeq(enabled)
        e_seq.fa.set_flow_style()
        entry["enabled"] = e_seq
    if disabled:
        d_seq = CommentedSeq(disabled)
        d_seq.fa.set_flow_style()
        entry["disabled"] = d_seq
    pos_seq = CommentedSeq(positions)
    pos_seq.fa.set_flow_style()
    entry["positions"] = pos_seq
    entry["main"] = main
    data["layouts"].append(entry)


def _update_layout(data, index: int, **changes) -> bool:
    """Update fields on a layout by index. Returns True if valid index."""
    layouts = data.get("layouts", [])
    if not 0 <= index < len(layouts):
        return False
    entry = layouts[index]
    from ruamel.yaml.comments import CommentedSeq
    _list_keys = ("match", "enabled", "disabled", "positions")
    _bool_keys = ("preferred",)
    for k, v in changes.items():
        if k in _list_keys:
            if v:
                seq = CommentedSeq(v)
                seq.fa.set_flow_style()
                entry[k] = seq
            elif k in entry:
                del entry[k]
        elif k in _bool_keys:
            if v:
                entry[k] = True
            elif k in entry:
                del entry[k]
        else:
            entry[k] = v
    return True


def _remove_layout(data, index: int) -> bool:
    """Remove a layout by index. Returns True if valid index."""
    layouts = data.get("layouts", [])
    if not 0 <= index < len(layouts):
        return False
    del layouts[index]
    return True


# ---------------------------------------------------------------------------
# Parsing
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


def run_displayplacer_list() -> str:
    try:
        result = subprocess.run(
            ["displayplacer", "list"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print(
            "Error: displayplacer not found. "
            "Install with: brew install displayplacer",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: displayplacer failed: {exc.stderr.strip()}", file=sys.stderr
        )
        raise SystemExit(1)
    return result.stdout


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
                value = line[len(prefix) + 1 :].strip()
                setattr(current, attr, value)
                break

    return displays


def extract_apply_command(output: str) -> str | None:
    for line in output.splitlines():
        if line.startswith("displayplacer "):
            return line.strip()
    return None


# ---------------------------------------------------------------------------
# Hardware identification (CoreDisplay + ioreg)
#
# Bridges CGDisplayID (contextual ID) → IOKit framebuffer → unique EDID
# alphanumeric serial, so identical monitors can be told apart even when
# they share the same numeric EDID serial.
# ---------------------------------------------------------------------------


def _query_ioregistry() -> dict[str, HWInfo]:
    """Map dispext name (e.g. 'dispext0') to HWInfo via ioreg."""
    try:
        raw = subprocess.run(
            ["ioreg", "-r", "-c", "AppleCLCD2", "-d", "1", "-a"],
            capture_output=True,
            check=True,
        ).stdout
        entries = plistlib.loads(raw)
    except Exception:
        return {}

    result: dict[str, HWInfo] = {}
    for entry in entries:
        name = entry.get("IONameMatched", "")
        dispext = name.split(",")[0] if "dispext" in name else ""
        if not dispext:
            continue
        prod = entry.get("DisplayAttributes", {}).get("ProductAttributes", {})
        if not prod:
            continue
        result[dispext] = HWInfo(
            alpha_serial=prod.get("AlphanumericSerialNumber", ""),
            product_name=prod.get("ProductName", ""),
            manufacturer_id=prod.get("ManufacturerID", ""),
            year_of_manufacture=prod.get("YearOfManufacture", 0),
            week_of_manufacture=prod.get("WeekOfManufacture", 0),
        )
    return result


def _query_coredisplay() -> dict[int, str]:
    """Map CGDisplayID (contextual ID) to dispext name via CoreDisplay private API."""
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

    max_displays = 16
    display_ids = (c_uint32 * max_displays)()
    count = c_uint32(0)
    if cg.CGGetActiveDisplayList(max_displays, display_ids, ctypes.byref(count)) != 0:
        return {}

    key_cf = cf.CFStringCreateWithCString(
        None, b"IODisplayLocation", kCFStringEncodingUTF8,
    )
    if not key_cf:
        return {}

    result: dict[int, str] = {}
    try:
        for i in range(count.value):
            ctx_id = display_ids[i]
            info_ptr = cd.CoreDisplay_DisplayCreateInfoDictionary(ctx_id)
            if not info_ptr:
                continue
            try:
                val_ptr = cf.CFDictionaryGetValue(info_ptr, key_cf)
                if not val_ptr:
                    continue
                cstr = cf.CFStringGetCStringPtr(val_ptr, kCFStringEncodingUTF8)
                if cstr:
                    location = cstr.decode("utf-8")
                else:
                    buf = ctypes.create_string_buffer(1024)
                    if not cf.CFStringGetCString(
                        val_ptr, buf, 1024, kCFStringEncodingUTF8,
                    ):
                        continue
                    location = buf.value.decode("utf-8")
                m = re.search(r"/(dispext\d+)@", location)
                if m:
                    result[int(ctx_id)] = m.group(1)
            finally:
                cf.CFRelease(info_ptr)
    finally:
        cf.CFRelease(key_cf)

    return result


# ---------------------------------------------------------------------------
# Disabled-display discovery and re-enable (private CGS API)
# ---------------------------------------------------------------------------


def _get_disabled_displays(
    fresh: bool = False,
) -> list[tuple[int, int, int, int, bool]]:
    """Return disabled-but-connected displays via the private CGSGetDisplayList API.

    Each tuple is (cg_display_id, serial, vendor, model, is_builtin).
    Virtual/phantom displays (vendor=0 AND serial=0) are filtered out.

    When *fresh* is True the query runs in a subprocess to avoid stale
    CGGetActiveDisplayList caches that accumulate in long-lived processes
    (e.g. the daemon).
    """
    if fresh:
        return _get_disabled_displays_subprocess()

    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        if not cg_path:
            return []
        cg = ctypes.cdll.LoadLibrary(cg_path)
    except OSError:
        return []

    c_uint32 = ctypes.c_uint32
    max_displays = 16

    cg.CGSGetDisplayList.argtypes = [
        c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
    ]
    cg.CGSGetDisplayList.restype = ctypes.c_int32
    cg.CGGetActiveDisplayList.argtypes = [
        c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32

    all_ids = (c_uint32 * max_displays)()
    all_cnt = c_uint32(0)
    if cg.CGSGetDisplayList(max_displays, all_ids, ctypes.byref(all_cnt)) != 0:
        return []

    active_ids = (c_uint32 * max_displays)()
    active_cnt = c_uint32(0)
    if cg.CGGetActiveDisplayList(max_displays, active_ids, ctypes.byref(active_cnt)) != 0:
        return []

    active_set = {active_ids[i] for i in range(active_cnt.value)}

    result: list[tuple[int, int, int, int, bool]] = []
    for i in range(all_cnt.value):
        d = all_ids[i]
        if d in active_set:
            continue
        serial = cg.CGDisplaySerialNumber(d)
        vendor = cg.CGDisplayVendorNumber(d)
        model = cg.CGDisplayModelNumber(d)
        builtin = bool(cg.CGDisplayIsBuiltin(d))
        if vendor == 0 and serial == 0:
            continue
        result.append((int(d), int(serial), int(vendor), int(model), builtin))
    return result


def _get_disabled_displays_subprocess() -> list[tuple[int, int, int, int, bool]]:
    """Run disabled-display discovery in a fresh subprocess.

    The daemon process may have stale CGGetActiveDisplayList caches after
    programmatic display configuration changes.  A fresh process sees the
    actual current state.
    """
    import json as _json

    script = (
        "import ctypes,ctypes.util,json,sys\n"
        "p=ctypes.util.find_library('CoreGraphics')\n"
        "if not p:print('[]');sys.exit()\n"
        "cg=ctypes.cdll.LoadLibrary(p);U=ctypes.c_uint32\n"
        "cg.CGSGetDisplayList.argtypes="
        "[U,ctypes.POINTER(U),ctypes.POINTER(U)]\n"
        "cg.CGSGetDisplayList.restype=ctypes.c_int32\n"
        "cg.CGGetActiveDisplayList.argtypes="
        "[U,ctypes.POINTER(U),ctypes.POINTER(U)]\n"
        "cg.CGGetActiveDisplayList.restype=ctypes.c_int32\n"
        "a=(U*16)();ac=U(0)\n"
        "if cg.CGSGetDisplayList(16,a,ctypes.byref(ac))!=0:"
        "print('[]');sys.exit()\n"
        "b=(U*16)();bc=U(0)\n"
        "if cg.CGGetActiveDisplayList(16,b,ctypes.byref(bc))!=0:"
        "print('[]');sys.exit()\n"
        "act={b[i] for i in range(bc.value)};r=[]\n"
        "for i in range(ac.value):\n"
        " d=a[i]\n"
        " if d in act:continue\n"
        " s=cg.CGDisplaySerialNumber(d);v=cg.CGDisplayVendorNumber(d)\n"
        " m=cg.CGDisplayModelNumber(d);bi=bool(cg.CGDisplayIsBuiltin(d))\n"
        " if v==0 and s==0:continue\n"
        " r.append([int(d),int(s),int(v),int(m),bi])\n"
        "print(json.dumps(r))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        return [
            (d, s, v, m, b)
            for d, s, v, m, b in _json.loads(result.stdout)
        ]
    except Exception:
        return []


def _disabled_display_objects(fresh: bool = False) -> list[Display]:
    """Build Display objects for disabled-but-connected displays.

    Uses CGSGetDisplayList (private API) to discover displays that macOS
    knows about but that are not active.  The serial is converted to the
    unsigned 's<num>' format that displayplacer uses so match_displays
    can pair them with known_screens.
    """
    result: list[Display] = []
    for cg_id, serial, _vendor, _model, _builtin in _get_disabled_displays(fresh):
        unsigned_serial = serial & 0xFFFFFFFF
        result.append(Display(
            contextual_id=str(cg_id),
            serial_id=f"s{unsigned_serial}",
            enabled="false",
        ))
    return result


def _reenable_displays(display_ids: list[int]) -> bool:
    """Re-enable the given CGDisplayIDs via a CGS configuration transaction."""
    if not display_ids:
        return True
    try:
        cg_path = ctypes.util.find_library("CoreGraphics")
        if not cg_path:
            return False
        cg = ctypes.cdll.LoadLibrary(cg_path)
    except OSError:
        return False

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
    if cg.CGBeginDisplayConfiguration(ctypes.byref(config)) != 0:
        return False

    for did in display_ids:
        rc = cg.CGSConfigureDisplayEnabled(config, c_uint32(did), True)
        if rc != 0:
            cg.CGCancelDisplayConfiguration(config)
            return False

    kCGConfigurePermanently = 2
    if cg.CGCompleteDisplayConfiguration(config, kCGConfigurePermanently) != 0:
        return False
    return True


def _query_coredisplay_subprocess() -> dict[int, str]:
    """Run the CoreDisplay query in a fresh subprocess.

    CoreGraphics caches the active display list within a process, so after
    programmatically re-enabling displays, CGGetActiveDisplayList in the
    current process returns stale data.  A fresh process sees the update.
    """
    import json as _json

    script = (
        "import ctypes,ctypes.util,re,json,sys\n"
        "cg=ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreGraphics'))\n"
        "cf=ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreFoundation'))\n"
        "cd=ctypes.cdll.LoadLibrary("
        "'/System/Library/Frameworks/CoreDisplay.framework/CoreDisplay')\n"
        "U=ctypes.c_uint32;V=ctypes.c_void_p;E=0x08000100\n"
        "cg.CGGetActiveDisplayList.argtypes=[U,ctypes.POINTER(U),ctypes.POINTER(U)]\n"
        "cg.CGGetActiveDisplayList.restype=ctypes.c_int32\n"
        "cd.CoreDisplay_DisplayCreateInfoDictionary.argtypes=[U]\n"
        "cd.CoreDisplay_DisplayCreateInfoDictionary.restype=V\n"
        "cf.CFStringCreateWithCString.argtypes=[V,ctypes.c_char_p,U]\n"
        "cf.CFStringCreateWithCString.restype=V\n"
        "cf.CFDictionaryGetValue.argtypes=[V,V];cf.CFDictionaryGetValue.restype=V\n"
        "cf.CFStringGetCStringPtr.argtypes=[V,U]\n"
        "cf.CFStringGetCStringPtr.restype=ctypes.c_char_p\n"
        "cf.CFStringGetCString.argtypes=[V,ctypes.c_char_p,ctypes.c_long,U]\n"
        "cf.CFStringGetCString.restype=ctypes.c_bool\n"
        "cf.CFRelease.argtypes=[V];cf.CFRelease.restype=None\n"
        "ids=(U*16)();cnt=U(0)\n"
        "if cg.CGGetActiveDisplayList(16,ids,ctypes.byref(cnt))!=0:"
        "print('{}');sys.exit()\n"
        "k=cf.CFStringCreateWithCString(None,b'IODisplayLocation',E)\n"
        "if not k:print('{}');sys.exit()\n"
        "r={}\n"
        "try:\n"
        " for i in range(cnt.value):\n"
        "  p=cd.CoreDisplay_DisplayCreateInfoDictionary(ids[i])\n"
        "  if not p:continue\n"
        "  try:\n"
        "   v=cf.CFDictionaryGetValue(p,k)\n"
        "   if not v:continue\n"
        "   s=cf.CFStringGetCStringPtr(v,E)\n"
        "   if s:loc=s.decode()\n"
        "   else:\n"
        "    b=ctypes.create_string_buffer(1024)\n"
        "    if not cf.CFStringGetCString(v,b,1024,E):continue\n"
        "    loc=b.value.decode()\n"
        "   m=re.search(r'/(dispext\\d+)@',loc)\n"
        "   if m:r[int(ids[i])]=m.group(1)\n"
        "  finally:cf.CFRelease(p)\n"
        "finally:cf.CFRelease(k)\n"
        "print(json.dumps(r))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        return {int(k): v for k, v in _json.loads(result.stdout).items()}
    except Exception:
        return {}


def build_hw_info_map(fresh: bool = False) -> dict[int, HWInfo]:
    """Map contextual display ID to HWInfo (EDID serial, product name, brand).

    Uses CoreDisplay to bridge CGDisplayID -> IOKit framebuffer path, then
    reads the ioreg to get hardware details.  Returns {} on failure.

    When *fresh* is True, runs the CoreDisplay query in a subprocess to
    avoid stale in-process CoreGraphics cache after re-enabling displays.
    """
    try:
        ioreg = _query_ioregistry()
        cd_map = _query_coredisplay_subprocess() if fresh else _query_coredisplay()
    except Exception:
        return {}
    if not ioreg or not cd_map:
        return {}
    return {
        ctx: ioreg[dispext]
        for ctx, dispext in cd_map.items()
        if dispext in ioreg
    }


# ---------------------------------------------------------------------------
# Screen matching
# ---------------------------------------------------------------------------


def _hw_matches_known(hw: HWInfo, known: KnownScreen) -> bool:
    """Check whether HW info satisfies the extra match criteria on a KnownScreen.

    Only fields that are explicitly set on the KnownScreen are checked.
    When HW data is unavailable (empty HWInfo), specified criteria cause a
    non-match — the config asked for something we can't verify.
    """
    if known.brand is not None:
        if not hw.manufacturer_id:
            return False
        resolved = _PNP_BRANDS.get(hw.manufacturer_id, hw.manufacturer_id)
        if known.brand.upper() not in (
            hw.manufacturer_id.upper(), resolved.upper(),
        ):
            return False
    if known.production_year is not None:
        if not hw.year_of_manufacture:
            return False
        if hw.year_of_manufacture != known.production_year:
            return False
    if known.production_week is not None:
        if not hw.week_of_manufacture:
            return False
        if hw.week_of_manufacture != known.production_week:
            return False
    if known.product_name is not None:
        if not hw.product_name:
            return False
        if hw.product_name.upper() != known.product_name.upper():
            return False
    return True


def match_displays(
    displays: list[Display],
    known_screens: dict[str, KnownScreen],
    hw_map: dict[int, HWInfo] | None = None,
) -> tuple[list[MatchedDisplay], bool]:
    """Match displays to known screens.

    Returns (matches, hw_resolved) where *hw_resolved* is True when the
    CoreDisplay identification chain succeeded in distinguishing monitors
    that share the same EDID serial.
    """
    if hw_map is None:
        hw_map = build_hw_info_map()

    alpha_index: dict[str, tuple[str, KnownScreen]] = {}
    for key, known in known_screens.items():
        if known.alpha_serial:
            alpha_index[known.alpha_serial] = (key, known)

    matched: list[MatchedDisplay] = []
    used_keys: set[str] = set()
    matched_ids: set[int] = set()
    hw_resolved = bool(hw_map)

    def _hw_for(d: Display) -> HWInfo:
        ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
        return hw_map.get(ctx, HWInfo()) if hw_map else HWInfo()

    # Pass 1: match by hardware serial (unique, reliable)
    if hw_map:
        for d in displays:
            ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
            alpha = hw_map.get(ctx, HWInfo()).alpha_serial
            if alpha and alpha in alpha_index:
                key, known = alpha_index[alpha]
                matched.append(MatchedDisplay(d, key, known, 1))
                used_keys.add(key)
                matched_ids.add(id(d))

    # Pass 2: match remaining by serial_id + optional HW criteria
    for d in displays:
        if id(d) in matched_ids:
            continue
        hw = _hw_for(d)
        for key, known in sorted(known_screens.items()):
            if key in used_keys or known.alpha_serial is not None:
                continue
            if d.serial_id == known.serial_id and _hw_matches_known(hw, known):
                matched.append(MatchedDisplay(d, key, known, 1))
                used_keys.add(key)
                matched_ids.add(id(d))
                break

    # Pass 3 fallback: hw lookup failed — assign alpha_serial screens by
    # serial_id with contextual-ID ordering (arbitrary but deterministic)
    if not hw_map:
        remaining: dict[str, list[Display]] = {}
        for d in displays:
            if id(d) not in matched_ids:
                remaining.setdefault(d.serial_id, []).append(d)
        for serial_id, group in remaining.items():
            candidates = sorted(
                [
                    (k, ks)
                    for k, ks in known_screens.items()
                    if ks.serial_id == serial_id and k not in used_keys
                ],
                key=lambda x: x[0],
            )
            if not candidates:
                continue
            group.sort(
                key=lambda d: (
                    int(d.contextual_id) if d.contextual_id.isdigit() else 0
                )
            )
            for (key, known), d in zip(candidates, group):
                matched.append(MatchedDisplay(d, key, known, 1))
                used_keys.add(key)

    matched.sort(key=lambda m: m.key)
    return matched, hw_resolved


def display_label(m: MatchedDisplay, all_matched: list[MatchedDisplay]) -> str:
    return m.key


# ---------------------------------------------------------------------------
# Command building  (uses contextual IDs — persistent UUIDs are not stable
# across wakeups, and screens with identical serials need contextual IDs to
# tell them apart)
# ---------------------------------------------------------------------------


def _resolve_settings(m: MatchedDisplay) -> tuple[str, int, int, str, str]:
    """Return (resolution, hertz, color_depth, scaling, enabled) for a matched display."""
    k, d = m.known, m.display

    res = k.resolution or d.resolution or "1920x1080"

    if k.hertz is None:
        hz = int(d.hertz) if d.hertz else 60
    else:
        cur = int(d.hertz) if d.hertz else 0
        if cur == k.hertz:
            hz = k.hertz
        elif k.fallback_hertz and cur == k.fallback_hertz:
            hz = k.fallback_hertz
        else:
            hz = k.hertz

    cd = k.color_depth if k.color_depth is not None else (int(d.color_depth) if d.color_depth else 8)
    sc = k.scaling or d.scaling or "off"
    en = k.enabled or d.enabled or "true"
    return res, hz, cd, sc, en


def build_command(layout: Layout, matched: list[MatchedDisplay]) -> list[str]:
    """Return displayplacer arg strings (one per display, without quotes)."""
    label_map: dict[str, MatchedDisplay] = {}
    for m in matched:
        label_map[display_label(m, matched)] = m

    main_res, *_ = _resolve_settings(label_map[layout.main])
    _, main_h = map(int, main_res.split("x"))

    widths: list[int] = []
    heights: list[int] = []
    for pos in layout.positions:
        res, *_ = _resolve_settings(label_map[pos])
        w, h = map(int, res.split("x"))
        widths.append(w)
        heights.append(h)

    main_idx = layout.positions.index(layout.main)
    origins_x = [0] * len(layout.positions)
    x = 0
    for i in range(main_idx - 1, -1, -1):
        x -= widths[i]
        origins_x[i] = x
    x = widths[main_idx]
    for i in range(main_idx + 1, len(layout.positions)):
        origins_x[i] = x
        x += widths[i]

    args: list[str] = []
    for i, pos in enumerate(layout.positions):
        m = label_map[pos]
        res, hz, cd, sc, _en = _resolve_settings(m)
        y = (main_h - heights[i]) // 2 if pos != layout.main else 0
        args.append(
            f"id:{m.display.contextual_id}"
            f" res:{res}"
            f" hz:{hz}"
            f" color_depth:{cd}"
            f" enabled:true"
            f" scaling:{sc}"
            f" origin:({origins_x[i]},{y})"
            f" degree:0"
        )

    for dis in layout.disabled:
        m = label_map.get(dis)
        if not m:
            continue
        d = m.display
        ox, oy = _parse_origin(d.origin)
        args.append(
            f"id:{d.contextual_id}"
            f" res:{d.resolution}"
            f" hz:{d.hertz}"
            f" color_depth:{d.color_depth}"
            f" enabled:false"
            f" scaling:{d.scaling}"
            f" origin:({ox},{oy})"
            f" degree:0"
        )

    mentioned = set(layout.positions) | set(layout.disabled)
    rightmost_x = max(origins_x[i] + widths[i] for i in range(len(layout.positions))) if widths else 0
    extra_x = rightmost_x
    for key, m in label_map.items():
        if key in mentioned:
            continue
        d = m.display
        if d.enabled == "false":
            continue
        res, hz, cd, sc, _en = _resolve_settings(m)
        w, h = map(int, res.split("x"))
        y = (main_h - h) // 2
        args.append(
            f"id:{d.contextual_id}"
            f" res:{res}"
            f" hz:{hz}"
            f" color_depth:{cd}"
            f" enabled:true"
            f" scaling:{sc}"
            f" origin:({extra_x},{y})"
            f" degree:0"
        )
        extra_x += w

    if args and not any(" enabled:true" in a for a in args):
        _log(
            "WARNING: no enabled displays — forcing main to enabled:true"
        )
        main_m = label_map.get(layout.main)
        if main_m:
            res, hz, cd, sc, _en = _resolve_settings(main_m)
            args = [
                a for a in args
                if not a.startswith(f"id:{main_m.display.contextual_id} ")
            ]
            args.insert(0, (
                f"id:{main_m.display.contextual_id}"
                f" res:{res}"
                f" hz:{hz}"
                f" color_depth:{cd}"
                f" enabled:true"
                f" scaling:{sc}"
                f" origin:(0,0)"
                f" degree:0"
            ))

    return args


def _strip_enabled_flag(args: list[str]) -> list[str]:
    return [re.sub(r' enabled:(?:true|false)', '', a) for a in args]


def format_command(args: list[str]) -> str:
    """Pretty-print a multi-line displayplacer command."""
    if len(args) <= 1:
        return "displayplacer " + " ".join(f'"{a}"' for a in args)
    lines = ["displayplacer \\"]
    for i, arg in enumerate(args):
        end = " \\" if i < len(args) - 1 else ""
        lines.append(f'  "{arg}"{end}')
    return "\n".join(lines)


def _build_reposition_args(
    layout: Layout,
    matched: list[MatchedDisplay],
) -> list[str]:
    """Build displayplacer args for the reposition phase.

    All displays remain enabled:true.  Target positions get their calculated
    origins; to-be-disabled displays are placed to the far right so the main
    display switch and window migration happen before anything is disabled.
    """
    label_map: dict[str, MatchedDisplay] = {}
    for m in matched:
        label_map[display_label(m, matched)] = m

    main_res, *_ = _resolve_settings(label_map[layout.main])
    _, main_h = map(int, main_res.split("x"))

    widths: list[int] = []
    heights: list[int] = []
    for pos in layout.positions:
        res, *_ = _resolve_settings(label_map[pos])
        w, h = map(int, res.split("x"))
        widths.append(w)
        heights.append(h)

    main_idx = layout.positions.index(layout.main)
    origins_x = [0] * len(layout.positions)
    x = 0
    for i in range(main_idx - 1, -1, -1):
        x -= widths[i]
        origins_x[i] = x
    x = widths[main_idx]
    for i in range(main_idx + 1, len(layout.positions)):
        origins_x[i] = x
        x += widths[i]

    args: list[str] = []
    for i, pos in enumerate(layout.positions):
        m = label_map[pos]
        res, hz, cd, sc, _en = _resolve_settings(m)
        y = (main_h - heights[i]) // 2 if pos != layout.main else 0
        args.append(
            f"id:{m.display.contextual_id}"
            f" res:{res}"
            f" hz:{hz}"
            f" color_depth:{cd}"
            f" enabled:true"
            f" scaling:{sc}"
            f" origin:({origins_x[i]},{y})"
            f" degree:0"
        )

    rightmost_x = (
        max(origins_x[i] + widths[i] for i in range(len(layout.positions)))
        if widths else 0
    )
    extra_x = rightmost_x

    for dis in layout.disabled:
        m = label_map.get(dis)
        if not m or (m.display.enabled == "false" and not m.display.resolution):
            continue
        res, hz, cd, sc, _en = _resolve_settings(m)
        w, h = map(int, res.split("x"))
        y = (main_h - h) // 2
        args.append(
            f"id:{m.display.contextual_id}"
            f" res:{res}"
            f" hz:{hz}"
            f" color_depth:{cd}"
            f" enabled:true"
            f" scaling:{sc}"
            f" origin:({extra_x},{y})"
            f" degree:0"
        )
        extra_x += w

    mentioned = set(layout.positions) | set(layout.disabled)
    for key, m in label_map.items():
        if key in mentioned:
            continue
        d = m.display
        if d.enabled == "false":
            continue
        res, hz, cd, sc, _en = _resolve_settings(m)
        w, h = map(int, res.split("x"))
        y = (main_h - h) // 2
        args.append(
            f"id:{d.contextual_id}"
            f" res:{res}"
            f" hz:{hz}"
            f" color_depth:{cd}"
            f" enabled:true"
            f" scaling:{sc}"
            f" origin:({extra_x},{y})"
            f" degree:0"
        )
        extra_x += w

    return args


# ---------------------------------------------------------------------------
# Visual layout (ASCII diagram)
# ---------------------------------------------------------------------------


def _parse_origin(origin: str) -> tuple[int, int]:
    m = re.match(r"\((-?\d+),\s*(-?\d+)\)", origin)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _short_type(display_type: str) -> str:
    m = re.match(r"(\d+)\s*inch", display_type)
    return f'{m.group(1)}"' if m else display_type


_CONNECTS: dict[str, frozenset[str]] = {
    " ": frozenset(),
    "─": frozenset({"L", "R"}),
    "│": frozenset({"U", "D"}),
    "┌": frozenset({"D", "R"}),
    "┐": frozenset({"D", "L"}),
    "└": frozenset({"U", "R"}),
    "┘": frozenset({"U", "L"}),
    "├": frozenset({"U", "D", "R"}),
    "┤": frozenset({"U", "D", "L"}),
    "┬": frozenset({"D", "L", "R"}),
    "┴": frozenset({"U", "L", "R"}),
    "┼": frozenset({"U", "D", "L", "R"}),
}
_CHAR_FOR = {d: ch for ch, d in _CONNECTS.items()}


def _merge_box(a: str, b: str) -> str:
    return _CHAR_FOR.get(
        _CONNECTS.get(a, frozenset()) | _CONNECTS.get(b, frozenset()), b
    )


@dataclass
class _Rect:
    display: Display
    col: int
    row: int
    w: int
    h: int


def show_layout(displays: list[Display]) -> None:
    items: list[tuple[Display, int, int, int, int]] = []
    for d in displays:
        try:
            pw, ph = map(int, d.resolution.split("x"))
        except (ValueError, AttributeError):
            continue
        ox, oy = _parse_origin(d.origin)
        items.append((d, ox, oy, pw, ph))

    if not items:
        return

    min_x = min(ox for _, ox, _, _, _ in items)
    max_x = max(ox + pw for _, ox, _, pw, _ in items)
    min_y = min(oy for _, _, oy, _, _ in items)
    max_y = max(oy + ph for _, _, oy, _, ph in items)

    total_px_w = max_x - min_x or 1
    TARGET_W, MIN_W, MIN_H = 76, 24, 7
    sx = TARGET_W / total_px_w
    sy = sx / 2.0

    rects = [
        _Rect(
            d,
            round((ox - min_x) * sx),
            round((oy - min_y) * sy),
            max(round(pw * sx), MIN_W),
            max(round(ph * sy), MIN_H),
        )
        for d, ox, oy, pw, ph in items
    ]

    gw = max(r.col + r.w for r in rects) + 1
    gh = max(r.row + r.h for r in rects) + 1
    grid = [[" "] * gw for _ in range(gh)]

    def put(row: int, col: int, ch: str) -> None:
        if 0 <= row < gh and 0 <= col < gw:
            grid[row][col] = _merge_box(grid[row][col], ch)

    rects.sort(key=lambda r: (r.col, r.row))

    for r in rects:
        x1, y1, x2, y2 = r.col, r.row, r.col + r.w, r.row + r.h
        for x in range(x1 + 1, x2):
            put(y1, x, "─")
            put(y2, x, "─")
        for y in range(y1 + 1, y2):
            put(y, x1, "│")
            put(y, x2, "│")
        put(y1, x1, "┌")
        put(y1, x2, "┐")
        put(y2, x1, "└")
        put(y2, x2, "┘")

        d = r.display
        main = " ★" if d.is_main else ""
        labels = [
            f"[{d.contextual_id}] {_short_type(d.type)}{main}",
            f"{d.resolution} @ {d.hertz}Hz",
        ]
        iw = r.w - 2
        ty = y1 + (r.h - len(labels)) // 2
        for li, text in enumerate(labels):
            tr = ty + li
            if y1 < tr < y2:
                text = text[:iw] if len(text) > iw else text
                pad = (iw - len(text)) // 2
                for j, ch in enumerate(text):
                    c = x1 + 1 + pad + j
                    if x1 < c < x2:
                        grid[tr][c] = ch

    print("\nDisplay layout:\n")
    for row in grid:
        line = "".join(row).rstrip()
        if line:
            print(f"  {line}")


# ---------------------------------------------------------------------------
# Overview + interactive layout selection
# ---------------------------------------------------------------------------


def _with_swapped_alternatives(
    layouts: list[Layout],
    known_screens: dict[str, KnownScreen],
) -> list[Layout]:
    """For displays sharing a serial, append layout variants with them swapped."""
    by_serial: dict[str, list[str]] = {}
    for key, ks in known_screens.items():
        by_serial.setdefault(ks.serial_id, []).append(key)
    swap_pairs = [
        (ids[0], ids[1]) for ids in by_serial.values() if len(ids) == 2
    ]
    if not swap_pairs:
        return layouts

    def _swap(lst: list[str]) -> list[str]:
        return [
            id_b if x == id_a else id_a if x == id_b else x
            for x in lst
        ]

    result = list(layouts)
    for lay in layouts:
        for id_a, id_b in swap_pairs:
            if id_a in lay.positions and id_b in lay.positions:
                result.append(Layout(
                    name=f"{lay.name} [{id_a} \u2194 {id_b}]",
                    positions=_swap(lay.positions),
                    main=lay.main,
                    match=_swap(lay.match),
                    enabled=_swap(lay.enabled),
                    disabled=_swap(lay.disabled),
                ))
    return result


def _wait_for_stabilization(
    target_keys: set[str],
    known_screens: dict[str, KnownScreen],
    delays: tuple[float, ...] = (1.0, 2.0, 3.0),
    require_resolution: bool = True,
) -> tuple[list[MatchedDisplay], set[str]]:
    """Wait until target displays are active and identifiable.

    Returns (matched, still_missing) where *still_missing* contains the
    target keys that could not be satisfied after all retries.  Uses a fresh
    subprocess for the HW info map to avoid stale CGS caches.
    """
    matched: list[MatchedDisplay] = []
    still_missing = set(target_keys)
    for attempt, delay in enumerate(delays, 1):
        time.sleep(delay)
        output = run_displayplacer_list()
        displays = parse_displays(output)
        hw_map = build_hw_info_map(fresh=True)
        matched, _ = match_displays(displays, known_screens, hw_map)
        if require_resolution:
            ready_keys = {
                m.key for m in matched
                if m.display.resolution and m.display.enabled != "false"
            }
        else:
            ready_keys = {m.key for m in matched}
        still_missing = target_keys - ready_keys
        if not still_missing:
            return matched, set()
        _log(
            f"  Waiting for displays ({attempt}/{len(delays)}): "
            f"{', '.join(sorted(still_missing))} not ready"
        )
    return matched, still_missing


def _apply_layout(
    layout: Layout,
    matched: list[MatchedDisplay],
    known_screens: dict[str, KnownScreen] | None = None,
    allow_disable: bool = True,
) -> int:
    needed_by_layout = set(layout.positions) | set(layout.disabled)
    has_disables = bool(layout.disabled)

    # --- Phase 1: Enable disabled displays needed by this layout -----------
    if known_screens is not None and allow_disable:
        matched_keys = {m.key for m in matched}
        missing = needed_by_layout - matched_keys
        disabled_cg_ids = [
            int(m.display.contextual_id)
            for m in matched
            if m.key in needed_by_layout
            and (m.display.enabled == "false" or not m.display.resolution)
        ]

        if missing or disabled_cg_ids:
            all_disabled = _get_disabled_displays(fresh=True)
            if missing:
                ids_to_enable = [cg_id for cg_id, *_ in all_disabled]
            else:
                all_disabled_set = {cg_id for cg_id, *_ in all_disabled}
                ids_to_enable = [
                    d for d in disabled_cg_ids if d in all_disabled_set
                ]

            if ids_to_enable:
                _log(
                    f"Phase 1: Re-enabling {len(ids_to_enable)} "
                    f"display(s)..."
                )
                ok = _reenable_displays(ids_to_enable)
                if ok:
                    matched, still_missing = _wait_for_stabilization(
                        needed_by_layout, known_screens,
                        delays=(2.0, 3.0, 5.0),
                        require_resolution=False,
                    )
                    if still_missing:
                        critical = still_missing & set(layout.positions)
                        if critical:
                            _log(
                                f"Displays re-enabled but critical "
                                f"displays not identified: "
                                f"{', '.join(sorted(critical))}"
                            )
                            _log(
                                "Skipping layout — macOS restored "
                                "the arrangement."
                            )
                            return 0
                        _log(
                            f"Non-critical displays not identified: "
                            f"{', '.join(sorted(still_missing))} "
                            f"— proceeding"
                        )
                else:
                    _log("Warning: could not re-enable displays.")

        # Wait for target displays to be fully active (have resolution)
        target_enabled = set(layout.positions)
        active_keys = {
            m.key for m in matched
            if m.display.resolution and m.display.enabled != "false"
        }
        not_yet_active = target_enabled - active_keys
        if not_yet_active:
            _log(
                f"Target displays not yet active: "
                f"{', '.join(sorted(not_yet_active))} — waiting"
            )
            matched, still_inactive = _wait_for_stabilization(
                target_enabled, known_screens,
                delays=(1.0, 2.0, 3.0),
                require_resolution=True,
            )
            if still_inactive:
                _log(
                    f"WARNING: target displays still not active: "
                    f"{', '.join(sorted(still_inactive))} "
                    f"— proceeding anyway"
                )

    # --- Phase 2: Reposition -----------------------------------------------
    if has_disables:
        reposition_args = _build_reposition_args(layout, matched)
        if not allow_disable:
            reposition_args = _strip_enabled_flag(reposition_args)
        print(f"\nLayout: {layout.name}")
        _log(
            "Phase 2: Repositioning (all displays enabled, "
            "to-be-disabled on right)..."
        )
        _log(format_command(reposition_args))
        try:
            result = subprocess.run(
                ["displayplacer", *reposition_args], check=False,
            )
        except FileNotFoundError:
            print("Error: displayplacer not found.", file=sys.stderr)
            return 1
        if result.returncode != 0:
            return result.returncode

        if known_screens is not None:
            matched, _ = _wait_for_stabilization(
                set(layout.positions), known_screens,
                delays=(1.0, 2.0, 3.0),
                require_resolution=True,
            )

        # --- Phase 3: Disable unwanted displays ---------------------------
        if allow_disable:
            disable_args = build_command(layout, matched)
            _log("Phase 3: Disabling unwanted displays...")
            _log(format_command(disable_args))
            try:
                result = subprocess.run(
                    ["displayplacer", *disable_args], check=False,
                )
            except FileNotFoundError:
                print("Error: displayplacer not found.", file=sys.stderr)
                return 1
            if result.returncode != 0:
                return result.returncode

            if known_screens is not None:
                _wait_for_stabilization(
                    set(layout.positions), known_screens,
                    delays=(1.0, 2.0),
                    require_resolution=True,
                )

        print(f"\nLayout applied: {layout.name}")
        return 0

    # No displays to disable — single displayplacer call is sufficient
    args = build_command(layout, matched)
    if not allow_disable:
        args = _strip_enabled_flag(args)
    print(f"\nLayout: {layout.name}\n")
    _log(format_command(args))
    print("\nApplying...")
    try:
        result = subprocess.run(["displayplacer", *args], check=False)
    except FileNotFoundError:
        print("Error: displayplacer not found.", file=sys.stderr)
        return 1
    return result.returncode


def _clean_rotation(rotation: str) -> str:
    m = re.match(r"(\d+)", rotation)
    return m.group(1) if m else rotation


def show_displays(
    known_screens: dict[str, KnownScreen],
) -> int:
    """Show connected displays (active and disabled) without applying any layout."""
    output = run_displayplacer_list()
    displays = parse_displays(output)
    displays.extend(_disabled_display_objects())

    if not displays:
        print("No displays found.")
        return 1

    show_layout(displays)

    hw_map = build_hw_info_map()
    matched, _hw_resolved = match_displays(displays, known_screens, hw_map)

    print(f"\nConnected displays: {len(displays)}\n")

    for i, d in enumerate(displays, 1):
        main_tag = " (main)" if d.is_main else ""
        origin = re.sub(r"\s*-\s*main display", "", d.origin)
        rotation = _clean_rotation(d.rotation)

        ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
        hw = hw_map.get(ctx, HWInfo())
        brand_model = hw.display_name
        size = _short_type(d.type)

        matched_m: MatchedDisplay | None = None
        for m in matched:
            if m.display is d:
                matched_m = m
                break

        is_disabled = d.enabled == "false" and not d.resolution

        if matched_m:
            label = display_label(matched_m, matched)
            desc = f"{brand_model} — {matched_m.known.label}" if brand_model else matched_m.known.label
            disabled_tag = "  [disabled]" if is_disabled else ""
            print(f"  ({label}) [{d.contextual_id}] {desc}{main_tag}{disabled_tag}")
        elif brand_model:
            print(f"  [{d.contextual_id}] {brand_model} — {size}{main_tag}")
        else:
            print(f"  [{d.contextual_id}] {d.type}{main_tag}")

        if is_disabled:
            print(f"      Enabled: false")
            print(f"      Contextual: {d.contextual_id}")
        else:
            print(
                f"      Resolution: {d.resolution} @ {d.hertz}Hz"
                f"  Color depth: {d.color_depth}  Scaling: {d.scaling}"
            )
            print(f"      Origin: {origin}  Rotation: {rotation}°  Enabled: {d.enabled}")
            print(f"      Persistent: {d.persistent_id}")
            print(f"      Contextual: {d.contextual_id}")

        print(f"      match:")
        print(f"        serial: {d.serial_id}")
        if hw.alpha_serial:
            print(f"        edid_serial: {hw.alpha_serial}")
        if hw.manufacturer_id:
            print(f"        brand: {hw.brand}")
        if hw.year_of_manufacture:
            print(f"        production_year: {hw.year_of_manufacture}")
        if hw.week_of_manufacture:
            print(f"        production_week: {hw.week_of_manufacture}")
        if hw.product_name:
            print(f"        product_name: {hw.product_name}")
        if i < len(displays):
            print()

    if matched:
        sig = tuple(sorted(m.key for m in matched))
        labels = [
            display_label(m, matched)
            for m in sorted(matched, key=lambda m: (m.key, m.instance))
        ]
        print(f"\nDetected device set: {', '.join(labels)}")

    return 0


# ---------------------------------------------------------------------------
# Non-interactive apply (used by daemon)
# ---------------------------------------------------------------------------


def apply_current_layout(
    known_screens: dict[str, KnownScreen],
    device_set_layouts: dict[tuple[str, ...], list[Layout]],
) -> tuple[int, str | None]:
    """Detect displays, match, and apply the single unambiguous layout.

    Non-interactive -- used by the daemon.
    Returns (returncode, layout_name) where layout_name is set on success.
    """
    try:
        output = run_displayplacer_list()
    except SystemExit:
        _log("displayplacer not available")
        return 1, None

    displays = parse_displays(output)
    if not displays:
        _log("No displays found")
        return 1, None

    hw_map = build_hw_info_map()
    matched, hw_resolved = match_displays(displays, known_screens, hw_map)

    if not matched:
        _log("No matched displays")
        return 1, None

    # Layout lookup uses the active-display signature
    sig = tuple(sorted(m.key for m in matched))
    layouts = device_set_layouts.get(sig, [])
    if not hw_resolved:
        layouts = _with_swapped_alternatives(layouts, known_screens)

    labels = [display_label(m, matched) for m in matched]

    if not layouts:
        _log(f"No layout for device set: {', '.join(labels)}")
        return 1, None

    if len(layouts) > 1:
        preferred_lay = next((l for l in layouts if l.preferred), None)
        if not preferred_lay:
            _log(f"Ambiguous layout ({len(layouts)} options, no preferred) — skipping")
            return 1, None
        layout = preferred_lay
    else:
        layout = layouts[0]

    # Extend with disabled displays so _apply_layout can manage enable/disable
    disabled_objs = _disabled_display_objects(fresh=True)
    if disabled_objs:
        all_displays = displays + disabled_objs
        matched, _ = match_displays(all_displays, known_screens, hw_map)

    _log(f"Applying: {', '.join(labels)} -> {layout.name}")
    rc = _apply_layout(layout, matched, known_screens, allow_disable=False)
    if rc == 0:
        _log("Layout applied")
        return 0, layout.name
    _log(f"Layout application failed with code {rc}")
    return rc, None


# ---------------------------------------------------------------------------
# Daemon mode (daemon)
# ---------------------------------------------------------------------------

_WAKE_DELAYS = (5, 10, 15)
_RECONFIG_DELAYS = (2, 5, 10)


def daemon_main(
    known_screens: dict[str, KnownScreen],
    device_set_layouts: dict[tuple[str, ...], list[Layout]],
    options: Options,
) -> int:
    """Stay resident, re-applying the layout on wake and display changes."""
    _log("Daemon starting — applying layout now")
    _rc, initial_name = apply_current_layout(known_screens, device_set_layouts)

    if options.enable_menu_bar:
        import rumps
        from PyObjCTools import AppHelper

    iokit_path = ctypes.util.find_library("IOKit")
    cf_path = ctypes.util.find_library("CoreFoundation")
    cg_path = ctypes.util.find_library("CoreGraphics")
    if not iokit_path or not cf_path or not cg_path:
        _log("Error: IOKit, CoreFoundation, or CoreGraphics not found")
        return 1

    iokit = ctypes.cdll.LoadLibrary(iokit_path)
    cf = ctypes.cdll.LoadLibrary(cf_path)
    cg = ctypes.cdll.LoadLibrary(cg_path)

    c_uint32 = ctypes.c_uint32
    c_void_p = ctypes.c_void_p

    _PowerCB = ctypes.CFUNCTYPE(None, c_void_p, c_uint32, c_uint32, c_void_p)
    _ReconfigCB = ctypes.CFUNCTYPE(None, c_uint32, c_uint32, c_void_p)

    cg.CGDisplayRegisterReconfigurationCallback.argtypes = [_ReconfigCB, c_void_p]
    cg.CGDisplayRegisterReconfigurationCallback.restype = ctypes.c_int32
    cg.CGDisplayRemoveReconfigurationCallback.argtypes = [_ReconfigCB, c_void_p]
    cg.CGDisplayRemoveReconfigurationCallback.restype = ctypes.c_int32

    iokit.IORegisterForSystemPower.argtypes = [
        c_void_p, ctypes.POINTER(c_void_p), _PowerCB, ctypes.POINTER(c_uint32),
    ]
    iokit.IORegisterForSystemPower.restype = c_uint32

    iokit.IONotificationPortGetRunLoopSource.argtypes = [c_void_p]
    iokit.IONotificationPortGetRunLoopSource.restype = c_void_p

    iokit.IOAllowPowerChange.argtypes = [c_uint32, ctypes.c_long]
    iokit.IOAllowPowerChange.restype = c_uint32

    cf.CFRunLoopGetCurrent.argtypes = []
    cf.CFRunLoopGetCurrent.restype = c_void_p
    cf.CFRunLoopAddSource.argtypes = [c_void_p, c_void_p, c_void_p]
    cf.CFRunLoopAddSource.restype = None
    cf.CFRunLoopRun.argtypes = []
    cf.CFRunLoopRun.restype = None
    cf.CFRunLoopStop.argtypes = [c_void_p]
    cf.CFRunLoopStop.restype = None

    kIOMessageCanSystemSleep = 0xE0000270
    kIOMessageSystemWillSleep = 0xE0000280
    kIOMessageSystemHasPoweredOn = 0xE0000300

    kCFRunLoopDefaultMode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

    apply_lock = threading.Lock()
    pending: list[threading.Timer] = []
    _suppress_reconfig = True

    def cancel_pending() -> None:
        for t in pending:
            t.cancel()
        pending.clear()

    # -- Menu bar app (only instantiated when enabled) --
    _menu_bar_active = options.enable_menu_bar
    app: object = None

    def _notify(title: str, subtitle: str) -> None:
        """Send a macOS notification via osascript (works on all macOS versions)."""
        try:
            script = 'display notification {} with title {}'.format(
                _applescript_quote(subtitle), _applescript_quote(title),
            )
            subprocess.run(["osascript", "-e", script], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _applescript_quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    if options.enable_menu_bar:
        _IDLE_TITLE = "\U0001F5A5\uFE0E"
        _BUSY_TITLE = "\u23F3"

        class LayoutMenuBarApp(rumps.App):
            def __init__(self, current_layout_name: str | None):
                super().__init__(
                    "DisplayPlacer",
                    title=_IDLE_TITLE,
                    quit_button=None,
                )
                self._current_layout_name = current_layout_name
                self._build_menu()

            def _all_layouts(self) -> list[Layout]:
                return [lay for group in device_set_layouts.values() for lay in group]

            def _build_menu(self) -> None:
                self.menu.clear()
                for lay in self._all_layouts():
                    item = rumps.MenuItem(lay.name, callback=self._on_layout_click)
                    if lay.name == self._current_layout_name:
                        item.state = 1
                    self.menu.add(item)
                self.menu.add(rumps.separator)
                self.menu.add(rumps.MenuItem("Reset displays", callback=self._on_reset_click))
                self.menu.add(rumps.separator)
                self.menu.add(rumps.MenuItem("Quit", callback=lambda _: rumps.quit_application()))

            def schedule_menu_rebuild(self) -> None:
                """Dispatch _build_menu to the main thread (safe from any thread)."""
                AppHelper.callAfter(self._build_menu)

            def _finish_operation(self) -> None:
                """Restore idle title and rebuild menu. Must run on main thread."""
                self.title = _IDLE_TITLE
                self._build_menu()

            def _schedule_finish(self) -> None:
                AppHelper.callAfter(self._finish_operation)

            def _on_layout_click(self, sender):
                if not apply_lock.acquire(blocking=False):
                    return
                layout = next((l for l in self._all_layouts() if l.name == sender.title), None)
                if not layout:
                    apply_lock.release()
                    return
                self.title = _BUSY_TITLE
                t = threading.Thread(target=self._do_apply, args=(layout,), daemon=True)
                t.start()

            def _do_apply(self, layout):
                try:
                    displays = parse_displays(run_displayplacer_list())
                    displays.extend(_disabled_display_objects(fresh=True))
                    hw_map = build_hw_info_map()
                    matched, _ = match_displays(displays, known_screens, hw_map)
                    if matched:
                        _apply_layout(layout, matched, known_screens)
                        self._current_layout_name = layout.name
                        _log(f"Menu: applied {layout.name}")
                        _notify("Layout applied", layout.name)
                    else:
                        _log("Menu: no matched displays")
                        _notify("Layout failed", "No matched displays")
                except Exception as exc:
                    _log(f"Menu: error applying layout: {exc}")
                    _notify("Layout failed", str(exc))
                finally:
                    apply_lock.release()
                    self._schedule_finish()

            def _on_reset_click(self, sender):
                if not apply_lock.acquire(blocking=False):
                    return
                self.title = _BUSY_TITLE
                t = threading.Thread(target=self._do_reset, daemon=True)
                t.start()

            def _do_reset(self):
                try:
                    disabled = _get_disabled_displays(fresh=True)
                    if disabled:
                        ids = [d for d, *_ in disabled]
                        _log(f"Menu: re-enabling {len(ids)} display(s)")
                        _reenable_displays(ids)
                        _notify("Displays reset", f"Re-enabled {len(ids)} display(s)")
                    else:
                        _log("Menu: all displays already active")
                        _notify("Displays reset", "All displays already active")
                except Exception as exc:
                    _log(f"Menu: reset error: {exc}")
                    _notify("Reset failed", str(exc))
                finally:
                    apply_lock.release()
                    self._schedule_finish()

        app = LayoutMenuBarApp(initial_name)

        # Global hotkey: Ctrl+Option+Cmd+R → reset displays
        from AppKit import NSEvent
        _HOTKEY_MASK_FLAGS = 0x40000 | 0x80000 | 0x100000  # Ctrl | Option | Cmd
        _HOTKEY_R_KEYCODE = 15

        def _global_key_handler(event):
            if (event.keyCode() == _HOTKEY_R_KEYCODE
                    and (event.modifierFlags() & _HOTKEY_MASK_FLAGS) == _HOTKEY_MASK_FLAGS):
                app._on_reset_click(None)

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            1 << 10,  # NSEventMaskKeyDown
            _global_key_handler,
        )

    def _unsuppress() -> None:
        nonlocal _suppress_reconfig
        _suppress_reconfig = False
        _log("Reconfig listener re-enabled (suppression lifted)")

    def safe_apply() -> None:
        nonlocal _suppress_reconfig
        if not apply_lock.acquire(blocking=False):
            _log("Layout apply already in progress — skipping")
            return
        _suppress_reconfig = True
        cancel_pending()
        try:
            _rc, name = apply_current_layout(known_screens, device_set_layouts)
            if _menu_bar_active:
                if name:
                    app._current_layout_name = name  # type: ignore[union-attr]
                app.schedule_menu_rebuild()  # type: ignore[union-attr]
        except Exception as exc:
            _log(f"Error: {exc}")
        finally:
            apply_lock.release()
            t = threading.Timer(5.0, _unsuppress)
            t.daemon = True
            t.start()

    root_port = c_uint32()
    notify_port = c_void_p()
    notifier = c_uint32()

    @_PowerCB
    def _power_cb(_refcon, _service, msg_type, msg_arg):
        if msg_type == kIOMessageSystemHasPoweredOn:
            _log("Wake detected — scheduling layout at 5/10/15s")
            cancel_pending()
            for delay in _WAKE_DELAYS:
                t = threading.Timer(delay, safe_apply)
                t.daemon = True
                t.start()
                pending.append(t)
        elif msg_type in (kIOMessageCanSystemSleep, kIOMessageSystemWillSleep):
            cancel_pending()
            iokit.IOAllowPowerChange(
                root_port.value, msg_arg if msg_arg is not None else 0,
            )

    kCGDisplayBeginConfigurationFlag = 1 << 0
    kCGDisplayAddFlag = 1 << 4
    kCGDisplayRemoveFlag = 1 << 5

    @_ReconfigCB
    def _reconfig_cb(_display, flags, _user_info):
        if flags & kCGDisplayBeginConfigurationFlag:
            return
        if _suppress_reconfig:
            return
        if flags & (kCGDisplayAddFlag | kCGDisplayRemoveFlag):
            _log("Display reconfiguration detected — scheduling layout at 2/5/10s")
            cancel_pending()
            for delay in _RECONFIG_DELAYS:
                t = threading.Timer(delay, safe_apply)
                t.daemon = True
                t.start()
                pending.append(t)

    root_port.value = iokit.IORegisterForSystemPower(
        None, ctypes.byref(notify_port), _power_cb, ctypes.byref(notifier),
    )
    if root_port.value == 0:
        _log("Error: IORegisterForSystemPower failed")
        return 1

    source = iokit.IONotificationPortGetRunLoopSource(notify_port)
    run_loop = cf.CFRunLoopGetCurrent()
    cf.CFRunLoopAddSource(run_loop, source, kCFRunLoopDefaultMode)

    if cg.CGDisplayRegisterReconfigurationCallback(_reconfig_cb, None) != 0:
        _log("Warning: CGDisplayRegisterReconfigurationCallback failed")

    def _cleanup():
        _log("Shutting down")
        cancel_pending()
        cg.CGDisplayRemoveReconfigurationCallback(_reconfig_cb, None)

    if _menu_bar_active:
        from PyObjCTools import MachSignals as _ms

        def _mach_shutdown(signum):
            _cleanup()
            rumps.quit_application()

        _ms.signal(signal.SIGTERM, _mach_shutdown)
        _ms.signal(signal.SIGINT, _mach_shutdown)
        rumps.events.before_quit.register(_cleanup)
    else:
        def _shutdown(signum, _frame):
            _cleanup()
            cf.CFRunLoopStop(run_loop)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

    _log("Listening for wake and display events")
    t = threading.Timer(5.0, _unsuppress)
    t.daemon = True
    t.start()
    if _menu_bar_active:
        _log("Menu bar active")
        app.run()  # type: ignore[union-attr]
    else:
        cf.CFRunLoopRun()
    _log("Daemon stopped")
    return 0


# ---------------------------------------------------------------------------
# LaunchAgent management
# ---------------------------------------------------------------------------

_AGENT_LABEL = "com.user.displayplacer"
_AGENT_LOG = "/tmp/displayplacer-daemon.log"


def _agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_AGENT_LABEL}.plist"


def _get_uid() -> str:
    return str(os.getuid())


def _get_agent_pid() -> int | None:
    """Return the PID of the running LaunchAgent daemon, or None."""
    result = subprocess.run(
        ["launchctl", "list", _AGENT_LABEL],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if '"PID"' in line:
                m = re.search(r"(\d+)", line)
                if m:
                    return int(m.group(1))
    return None


def _wait_for_exit(pid: int, timeout: float = 5.0) -> bool:
    """Poll until *pid* exits. Returns True if it exited, False if still alive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def install_launch_agent() -> int:
    """Generate, write, and load a macOS LaunchAgent for the daemon."""
    uv_path = shutil.which("uv")
    if not uv_path:
        print("Error: uv not found in PATH", file=sys.stderr)
        return 1

    script_path = str(Path(__file__).resolve())
    plist_path = _agent_plist_path()

    plist_data = {
        "Label": _AGENT_LABEL,
        "ProgramArguments": [uv_path, "run", "--script", script_path, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": _AGENT_LOG,
        "StandardErrorPath": _AGENT_LOG,
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
    }

    # Unregister any existing service (running or not)
    pid = _get_agent_pid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{_get_uid()}/{_AGENT_LABEL}"],
        capture_output=True,
    )
    if pid is not None:
        _wait_for_exit(pid)

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{_get_uid()}", str(plist_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"Error: launchctl bootstrap failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    print("LaunchAgent installed and loaded:")
    print(f"  Plist:  {plist_path}")
    print(f"  Log:    {_AGENT_LOG}")
    print(f"  Label:  {_AGENT_LABEL}")
    print()
    print("The daemon will start on login and re-apply layout on wake.")
    print(f"To remove: {script_path} uninstall")
    return 0


def uninstall_launch_agent() -> int:
    """Stop the daemon and remove the macOS LaunchAgent."""
    plist_path = _agent_plist_path()

    if not plist_path.exists():
        print(f"LaunchAgent not found: {plist_path}")
        return 1

    # Unregister the service (running or not)
    pid = _get_agent_pid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{_get_uid()}/{_AGENT_LABEL}"],
        capture_output=True,
    )
    if pid is not None and not _wait_for_exit(pid):
        os.kill(pid, signal.SIGKILL)

    plist_path.unlink()
    print(f"LaunchAgent uninstalled: {plist_path}")
    return 0


def stop_launch_agent() -> int:
    """Stop the daemon process without unregistering the service."""
    pid = _get_agent_pid()
    if pid is None:
        print("Daemon is not running.")
        return 1

    subprocess.run(
        ["launchctl", "kill", "SIGTERM", f"gui/{_get_uid()}/{_AGENT_LABEL}"],
        capture_output=True,
    )

    if not _wait_for_exit(pid):
        os.kill(pid, signal.SIGKILL)
        print("Daemon force-killed.")
    else:
        print("Daemon stopped.")

    print(f"To restart:  {Path(__file__).resolve()} start")
    print(f"To remove:   {Path(__file__).resolve()} uninstall")
    return 0


def start_launch_agent() -> int:
    """Start the daemon process (service must already be installed)."""
    plist_path = _agent_plist_path()

    if not plist_path.exists():
        print("LaunchAgent not installed.", file=sys.stderr)
        print(f"Run '{Path(__file__).resolve()} install' first.")
        return 1

    if _get_agent_pid() is not None:
        print("Daemon is already running.")
        return 0

    target = f"gui/{_get_uid()}/{_AGENT_LABEL}"
    result = subprocess.run(
        ["launchctl", "kickstart", target],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Error: launchctl kickstart failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    print("Daemon started.")
    print(f"To check:  {Path(__file__).resolve()} status")
    return 0


def status_launch_agent() -> int:
    """Show whether the LaunchAgent is installed and the daemon is running."""
    plist_path = _agent_plist_path()
    installed = plist_path.exists()
    pid = _get_agent_pid()

    print(f"Plist:     {plist_path}")
    print(f"Installed: {'yes' if installed else 'no'}")
    print(f"Running:   {'yes (PID ' + str(pid) + ')' if pid else 'no'}")
    print(f"Log:       {_AGENT_LOG}")
    return 0


# ---------------------------------------------------------------------------
# Interactive config (config)
# ---------------------------------------------------------------------------


def _prompt(label: str, default: str = "") -> str:
    """Prompt user with an optional default shown in brackets."""
    if default:
        val = input(f"  {label} [{default}]: ").strip()
        return val or default
    return input(f"  {label}: ").strip()


def _confirm(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {label} [{hint}] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not val:
        return default
    return val.startswith("y")


def _suggest_id(hw: HWInfo, display: Display) -> str:
    """Generate a suggested display ID from brand/size info."""
    brand = _PNP_BRANDS.get(hw.manufacturer_id, "").lower()
    size = ""
    m = re.match(r"(\d+)\s*inch", display.type)
    if m:
        size = m.group(1)
    parts = [p for p in (brand, size) if p]
    return "-".join(parts) if parts else "display"


_INIT_FILE_HEADER = """\
# =============================================================================
# Display arrangement config for display-layout-manager.py
#
# This file defines your known monitors and how they should be arranged.
# Run 'display-layout-manager.py' (with no flags) to detect displays and apply a layout.
#
# TIP: Run 'display-layout-manager.py' with no arguments first — each display prints
#      a match: block with all available fields that you can copy directly into this file.
#
# YAML GOTCHAS:
#   - Purely numeric values like edid_serial must be quoted: "9876543210123"
#   - Use true/false for booleans (not on/off — YAML treats bare 'on' as true)
# ============================================================================="""

_INIT_DISPLAYS_HEADER = """\
# -----------------------------------------------------------------------------
# displays: list of known monitors
#
# Each entry needs:
#   id          - your chosen name, used to reference this display in layouts
#   label       - human-readable description (for display in output)
#   match       - how to identify this monitor when connected
#     serial          - Serial screen id from 'displayplacer list' (always required)
#     edid_serial     - EDID AlphanumericSerialNumber from ioreg (optional, needed
#                       only to distinguish identical monitors with the same serial)
#     brand           - manufacturer name, e.g. "ASUS", "Dell" (or PNP code "AUS")
#     production_year - year of manufacture from EDID, e.g. 2021
#     production_week - week of manufacture from EDID (1-53)
#     product_name    - model name from EDID, e.g. "VG28UQL1A"
#   settings    - (optional) preferred display mode; omit to keep current values
#     resolution    - e.g. "2560x1440"
#     hertz         - preferred refresh rate
#     fallback_hertz - used if preferred hertz isn't available
#     color_depth   - e.g. 8
#     scaling       - true or false
#     enabled       - true or false (default: true) — set false to disable display
# -----------------------------------------------------------------------------"""

_INIT_LAYOUTS_HEADER = """\
# -----------------------------------------------------------------------------
# layouts: physical arrangements to apply
#
# Each layout defines:
#   match     - (optional) which enabled displays trigger this layout for
#               auto / daemon; if omitted the layout never auto-matches
#               (manual selection via switch only)
#   preferred - (optional) true to auto-apply when multiple layouts share the
#               same match set; at most one per match set
#   enabled   - (optional) which displays should be active;
#               if omitted, defaults to the same set as positions
#   disabled  - (optional) display ids to set enabled:false for this layout;
#               allows switching between e.g. 1 active and 3 active monitors
#   positions - display ids in left-to-right physical order (required)
#   main      - which display sits at origin (0,0); others are placed relative
#
# Simple layouts only need 'positions' + 'main'.  When a layout has disabled
# displays, add 'match' to make the target device set explicit.
#
# At least one display must be in 'positions' (always-enabled safety rule).
# -----------------------------------------------------------------------------"""


def init_main(config_path: Path) -> int:
    """Detect connected displays and generate a fresh config.yml."""
    import io
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    print(
        "\n"
        "  This will detect all currently connected displays and generate\n"
        f"  a new {config_path.name} with:\n"
        "\n"
        "    - a display entry for every connected monitor\n"
        "    - a single layout using all enabled displays\n"
        "\n"
        "  You can fine-tune the result afterwards with the 'config' command.\n"
    )

    if config_path.exists():
        print(
            f"  WARNING: {config_path.name} already exists and will be overwritten.\n"
            "  A timestamped backup will be created first.\n"
        )
        if not _confirm("Continue?"):
            print("  Aborted.")
            return 1
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_suffix(f".yml.{ts}.bak")
        shutil.copy2(config_path, backup)
        print(f"  Backup saved to {backup.name}\n")
    else:
        if not _confirm("Continue?"):
            print("  Aborted.")
            return 1
        print()

    output = run_displayplacer_list()
    displays = parse_displays(output)
    disabled = _disabled_display_objects()
    all_displays = displays + disabled
    hw_map = build_hw_info_map()

    if not all_displays:
        print("No displays detected.", file=sys.stderr)
        return 1

    data = CommentedMap()
    data["displays"] = CommentedSeq()
    data["layouts"] = CommentedSeq()
    ry = YAML()

    assigned_ids: set[str] = set()
    display_info: list[tuple[Display, str, str]] = []  # (display, id, label)

    for d in all_displays:
        ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
        hw = hw_map.get(ctx, HWInfo())

        base_id = _suggest_id(hw, d)
        did = base_id
        suffix = 1
        while did in assigned_ids:
            suffix += 1
            did = f"{base_id}-{suffix}"
        assigned_ids.add(did)

        label = hw.display_name or _short_type(d.type)
        edid = hw.alpha_serial or None
        brand_name = hw.brand if hw.manufacturer_id else None
        prod_year = hw.year_of_manufacture or None
        prod_week = hw.week_of_manufacture or None
        prod_name = hw.product_name or None

        settings: dict | None = None
        if d.resolution:
            settings = {}
            settings["resolution"] = d.resolution
            if d.hertz:
                settings["hertz"] = int(d.hertz)
            if d.color_depth:
                settings["color_depth"] = int(d.color_depth)
            if d.scaling:
                settings["scaling"] = d.scaling.lower() == "on"
            settings["enabled"] = d.enabled.lower() != "false"

        _add_display(
            data, did=did, label=label, serial=d.serial_id,
            edid_serial=edid, brand=brand_name,
            production_year=prod_year, production_week=prod_week,
            product_name=prod_name, settings=settings,
        )
        display_info.append((d, did, label))

    enabled_info = [
        (d, did, label) for d, did, label in display_info
        if d.enabled.lower() != "false"
    ]
    disabled_ids = [
        did for d, did, _label in display_info
        if d.enabled.lower() == "false"
    ]

    if enabled_info:
        sorted_enabled = sorted(
            enabled_info, key=lambda t: _parse_origin(t[0].origin)[0],
        )
        position_ids = [did for _d, did, _label in sorted_enabled]
        main_id = next(
            (did for d, did, _label in sorted_enabled if d.is_main),
            position_ids[0],
        )
        layout_name = " | ".join(position_ids)
        _add_layout(
            data,
            name=layout_name,
            match_ids=list(assigned_ids),
            is_preferred=True,
            enabled=position_ids,
            disabled=disabled_ids or None,
            positions=position_ids,
            main=main_id,
        )

    # Dump YAML, then insert section-header comments.
    stream = io.StringIO()
    ry.dump(data, stream)
    raw = stream.getvalue()

    raw = (
        _INIT_FILE_HEADER + "\n\n\n"
        + _INIT_DISPLAYS_HEADER + "\n\n"
        + raw
    )
    raw = raw.replace(
        "\nlayouts:\n",
        "\n\n\n" + _INIT_LAYOUTS_HEADER + "\n\nlayouts:\n",
        1,
    )

    config_path.write_text(raw)

    n_disp = len(display_info)
    n_lay = len(data["layouts"])
    print(f"  Created {config_path.name} with {n_disp} display(s) and {n_lay} layout(s).\n")
    print("  Tip: use 'config' to fine-tune display settings and add more layouts.")
    return 0


def setup_main(config_path: Path) -> int:
    """Interactive display and layout setup menu loop."""
    output = run_displayplacer_list()
    displays = parse_displays(output)
    displays.extend(_disabled_display_objects())
    hw_map = build_hw_info_map()

    while True:
        data, ry = _load_raw_config(config_path)
        known_screens, device_set_layouts, _ = load_config(config_path)
        matched, _hw = match_displays(displays, known_screens, hw_map)

        matched_ctx: dict[str, MatchedDisplay] = {}
        for m_item in matched:
            matched_ctx[m_item.display.contextual_id] = m_item

        print("\n=== Display Setup ===\n")
        print("Connected displays:\n")
        num = 0
        display_index: list[tuple[Display, MatchedDisplay | None]] = []
        for d in displays:
            num += 1
            m_item = matched_ctx.get(d.contextual_id)
            ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
            hw = hw_map.get(ctx, HWInfo())
            if m_item:
                name = m_item.known.label
                status = "matched"
                print(f"  {num}. ({m_item.key})  {name:<36} {status}")
            else:
                name = hw.display_name or _short_type(d.type)
                status = "not in config"
                print(f"  {num}. {'':10}{name:<36} {status}")
            display_index.append((d, m_item))

        config_only = [
            (did, ks) for did, ks in known_screens.items()
            if not any(m_item.key == did for m_item in matched)
        ]
        if config_only:
            print("\nConfig-only (not connected):")
            for did, ks in config_only:
                num += 1
                print(f"  {num}. ({did})  {ks.label:<36} not connected")

        print()
        print("  [a] Add a display to config")
        print("  [e] Edit a display's settings")
        print("  [d] Delete a display from config")
        print("  [l] Manage layouts")
        print("  [q] Quit")
        print()

        try:
            choice = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "q":
            return 0
        elif choice == "a":
            _setup_add_display(
                config_path, data, ry, displays, matched, hw_map, known_screens,
            )
        elif choice == "e":
            _setup_edit_display(config_path, data, ry, known_screens)
        elif choice == "d":
            _setup_delete_display(config_path, data, ry, known_screens)
        elif choice == "l":
            _setup_layout_menu(config_path, known_screens)
        else:
            print("Invalid choice.")


def _setup_add_display(
    config_path: Path, data, ry,
    displays: list[Display],
    matched: list[MatchedDisplay],
    hw_map: dict[int, HWInfo],
    known_screens: dict[str, KnownScreen],
) -> None:
    """Add an unmatched connected display to config."""
    matched_ids = {id(m.display) for m in matched}
    unmatched = [d for d in displays if id(d) not in matched_ids]

    if not unmatched:
        print("\n  All connected displays are already in the config.")
        return

    print("\n  Unmatched displays:\n")
    for i, d in enumerate(unmatched, 1):
        ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
        hw = hw_map.get(ctx, HWInfo())
        name = hw.display_name or _short_type(d.type)
        print(f"    [{i}] {name}  (serial: {d.serial_id})")

    print()
    try:
        pick = input(f"  Select display to add [1-{len(unmatched)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick.isdigit() or not 1 <= int(pick) <= len(unmatched):
        print("  Cancelled.")
        return

    d = unmatched[int(pick) - 1]
    ctx = int(d.contextual_id) if d.contextual_id.isdigit() else 0
    hw = hw_map.get(ctx, HWInfo())

    suggested_id = _suggest_id(hw, d)
    existing_ids = set(known_screens.keys())
    suffix = 1
    base_id = suggested_id
    while suggested_id in existing_ids:
        suffix += 1
        suggested_id = f"{base_id}-{suffix}"

    did = _prompt("ID", suggested_id)
    label = _prompt("Label", hw.display_name or "")

    edid = hw.alpha_serial or None
    brand_name = hw.brand if hw.manufacturer_id else None
    prod_year = hw.year_of_manufacture or None
    prod_week = hw.week_of_manufacture or None
    prod_name = hw.product_name or None

    print(f"\n  Serial: {d.serial_id}")
    if edid:
        print(f"  EDID Serial: {edid}")
    if brand_name:
        print(f"  Brand: {brand_name}")
    if prod_name:
        print(f"  Product: {prod_name}")
    if prod_year:
        week_str = f"/W{prod_week}" if prod_week else ""
        print(f"  Produced: {prod_year}{week_str}")

    settings: dict | None = None
    if _confirm("Use current display settings?", default=True):
        settings = {}
        settings["resolution"] = d.resolution
        settings["hertz"] = int(d.hertz)
        settings["color_depth"] = int(d.color_depth)
        settings["scaling"] = d.scaling.lower() == "on"
        settings["enabled"] = d.enabled.lower() == "true"

    _add_display(
        data, did=did, label=label, serial=d.serial_id,
        edid_serial=edid, brand=brand_name,
        production_year=prod_year, production_week=prod_week,
        product_name=prod_name, settings=settings,
    )
    _save_raw_config(config_path, data, ry)
    print(f"\n  Display '{did}' added to config.")


def _setup_edit_display(
    config_path: Path, data, ry,
    known_screens: dict[str, KnownScreen],
) -> None:
    """Edit settings of an existing config display."""
    ids = list(known_screens.keys())
    if not ids:
        print("\n  No displays in config.")
        return

    print("\n  Config displays:\n")
    for i, did in enumerate(ids, 1):
        print(f"    [{i}] ({did}) {known_screens[did].label}")

    print()
    try:
        pick = input(f"  Select display to edit [1-{len(ids)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick.isdigit() or not 1 <= int(pick) <= len(ids):
        print("  Cancelled.")
        return

    did = ids[int(pick) - 1]
    ks = known_screens[did]
    print(f"\n  Editing '{did}' — press Enter to keep current value\n")

    changes: dict = {}
    new_label = _prompt("Label", ks.label)
    if new_label != ks.label:
        changes["label"] = new_label

    new_res = _prompt("Resolution", ks.resolution or "")
    if new_res != (ks.resolution or ""):
        changes["resolution"] = new_res if new_res else None

    new_hz = _prompt("Hertz", str(ks.hertz) if ks.hertz else "")
    if new_hz and new_hz != str(ks.hertz or ""):
        changes["hertz"] = int(new_hz)

    new_fbhz = _prompt("Fallback hertz", str(ks.fallback_hertz) if ks.fallback_hertz else "")
    if new_fbhz and new_fbhz != str(ks.fallback_hertz or ""):
        changes["fallback_hertz"] = int(new_fbhz)

    new_cd = _prompt("Color depth", str(ks.color_depth) if ks.color_depth else "")
    if new_cd and new_cd != str(ks.color_depth or ""):
        changes["color_depth"] = int(new_cd)

    cur_sc = {None: "", "on": "true", "off": "false"}.get(ks.scaling, ks.scaling or "")
    new_sc = _prompt("Scaling (true/false)", cur_sc)
    if new_sc and new_sc != cur_sc:
        changes["scaling"] = new_sc.lower() in ("true", "yes", "on", "1")

    cur_en = {None: "", "true": "true", "false": "false"}.get(ks.enabled, ks.enabled or "")
    new_en = _prompt("Enabled (true/false)", cur_en)
    if new_en and new_en != cur_en:
        changes["enabled"] = new_en.lower() in ("true", "yes", "on", "1")

    if changes:
        _update_display(data, did, **changes)
        _save_raw_config(config_path, data, ry)
        print(f"\n  Display '{did}' updated.")
    else:
        print("\n  No changes.")


def _setup_delete_display(
    config_path: Path, data, ry,
    known_screens: dict[str, KnownScreen],
) -> None:
    """Delete a display from config, cleaning up layout references."""
    ids = list(known_screens.keys())
    if not ids:
        print("\n  No displays in config.")
        return

    print("\n  Config displays:\n")
    for i, did in enumerate(ids, 1):
        print(f"    [{i}] ({did}) {known_screens[did].label}")

    print()
    try:
        pick = input(f"  Select display to delete [1-{len(ids)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick.isdigit() or not 1 <= int(pick) <= len(ids):
        print("  Cancelled.")
        return

    did = ids[int(pick) - 1]

    referencing = []
    for lay in data.get("layouts", []):
        all_refs = set()
        for key in ("match", "enabled", "positions", "disabled"):
            all_refs.update(str(x) for x in lay.get(key, []))
        if did in all_refs:
            referencing.append(lay.get("name", "?"))

    if referencing:
        print(f"\n  WARNING: '{did}' is referenced by layouts:")
        for name in referencing:
            print(f"    - {name}")

    if not _confirm(f"Delete '{did}'?"):
        print("  Cancelled.")
        return

    _remove_display(data, did)
    _save_raw_config(config_path, data, ry)
    print(f"\n  Display '{did}' deleted.")


# ---------------------------------------------------------------------------
# Layout management sub-menu (part of config)
# ---------------------------------------------------------------------------


def _setup_layout_menu(
    config_path: Path,
    known_screens: dict[str, KnownScreen],
) -> None:
    """Layout management sub-menu loop."""
    while True:
        data, ry = _load_raw_config(config_path)
        raw_layouts = data.get("layouts", [])

        print("\n=== Layout Management ===\n")
        print("Layouts:")
        if not raw_layouts:
            print("  (none)")
        for i, lay in enumerate(raw_layouts, 1):
            name = lay.get("name", f"Layout #{i}")
            is_pref = lay.get("preferred", False)
            tag = "  * preferred" if is_pref else ""
            match_ids = [str(m) for m in lay.get("match", [])]
            enabled = [str(e) for e in lay.get("enabled", lay.get("positions", []))]
            disabled = [str(d) for d in lay.get("disabled", [])]
            print(f"  {i}. {name}{tag}")
            if match_ids:
                print(f"     match: {', '.join(match_ids)}")
            print(f"     enabled: {', '.join(enabled)}")
            if disabled:
                print(f"     disabled: {', '.join(disabled)}")

        print()
        print("  [a] Add a layout")
        print("  [e] Edit a layout")
        print("  [d] Delete a layout")
        print("  [b] Back")
        print()

        try:
            choice = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "b":
            return
        elif choice == "a":
            _setup_add_layout(config_path, data, ry, known_screens)
        elif choice == "e":
            _setup_edit_layout(config_path, data, ry, known_screens)
        elif choice == "d":
            _setup_delete_layout(config_path, data, ry)
        else:
            print("Invalid choice.")


def _setup_add_layout(
    config_path: Path, data, ry,
    known_screens: dict[str, KnownScreen],
) -> None:
    """Add a new layout via interactive prompts."""
    all_ids = list(known_screens.keys())
    print(f"\n  Available display IDs: {', '.join(all_ids)}\n")

    name = _prompt("Layout name")
    if not name:
        print("  Cancelled.")
        return

    pos_raw = _prompt("Enabled displays (comma-separated, left to right)")
    positions = [p.strip() for p in pos_raw.split(",") if p.strip()]
    if not positions:
        print("  At least one enabled display is required. Cancelled.")
        return
    for p in positions:
        if p not in known_screens:
            print(f"  Unknown display '{p}'. Cancelled.")
            return

    dis_raw = _prompt("Disabled displays (comma-separated, or Enter for none)", "")
    disabled = [d.strip() for d in dis_raw.split(",") if d.strip()] if dis_raw else []
    for d in disabled:
        if d not in known_screens:
            print(f"  Unknown display '{d}'. Cancelled.")
            return

    auto_match = sorted(set(positions) | set(disabled))
    match_str = ", ".join(auto_match)
    match_raw = _prompt("Match set (Enter for auto)", match_str)
    match_ids = [m.strip() for m in match_raw.split(",") if m.strip()]

    main = _prompt("Main display", positions[0])
    if main not in positions:
        print(f"  Main '{main}' is not in enabled displays. Cancelled.")
        return

    is_preferred = _confirm("Set as preferred for this match set?")

    explicit_match = match_ids if sorted(match_ids) != auto_match else None
    explicit_enabled = positions if disabled else None

    _add_layout(
        data, name=name, positions=positions, main=main,
        match_ids=explicit_match, enabled=explicit_enabled,
        disabled=disabled, is_preferred=is_preferred,
    )
    _save_raw_config(config_path, data, ry)
    print(f"\n  Layout '{name}' added.")


def _setup_edit_layout(
    config_path: Path, data, ry,
    known_screens: dict[str, KnownScreen],
) -> None:
    """Edit an existing layout via interactive prompts."""
    raw_layouts = data.get("layouts", [])
    if not raw_layouts:
        print("\n  No layouts to edit.")
        return

    print()
    for i, lay in enumerate(raw_layouts, 1):
        print(f"    [{i}] {lay.get('name', f'Layout #{i}')}")

    print()
    try:
        pick = input(f"  Select layout to edit [1-{len(raw_layouts)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick.isdigit() or not 1 <= int(pick) <= len(raw_layouts):
        print("  Cancelled.")
        return

    idx = int(pick) - 1
    lay = raw_layouts[idx]
    all_ids = list(known_screens.keys())
    print(f"\n  Available display IDs: {', '.join(all_ids)}")
    print("  Press Enter to keep current value\n")

    changes: dict = {}

    cur_name = lay.get("name", "")
    new_name = _prompt("Name", cur_name)
    if new_name != cur_name:
        changes["name"] = new_name

    cur_match = ", ".join(str(m) for m in lay.get("match", []))
    new_match_raw = _prompt("Match set (comma-separated)", cur_match)
    new_match = [m.strip() for m in new_match_raw.split(",") if m.strip()] if new_match_raw else []
    if new_match_raw != cur_match:
        changes["match"] = new_match

    cur_pref = bool(lay.get("preferred", False))
    new_pref = _confirm("Set as preferred?", default=cur_pref)
    if new_pref != cur_pref:
        changes["preferred"] = new_pref

    cur_pos = ", ".join(str(p) for p in lay.get("positions", []))
    new_pos_raw = _prompt("Positions (left to right)", cur_pos)
    new_positions = [p.strip() for p in new_pos_raw.split(",") if p.strip()]
    if not new_positions:
        print("  At least one enabled display is required. Cancelled.")
        return
    if new_pos_raw != cur_pos:
        changes["positions"] = new_positions

    cur_en = ", ".join(str(e) for e in lay.get("enabled", lay.get("positions", [])))
    new_en_raw = _prompt("Enabled displays (comma-separated)", cur_en)
    new_enabled = [e.strip() for e in new_en_raw.split(",") if e.strip()]
    if new_en_raw != cur_en:
        changes["enabled"] = new_enabled

    cur_dis = ", ".join(str(d) for d in lay.get("disabled", []))
    new_dis_raw = _prompt("Disabled displays (comma-separated)", cur_dis)
    new_disabled = [d.strip() for d in new_dis_raw.split(",") if d.strip()] if new_dis_raw else []
    if new_dis_raw != cur_dis:
        changes["disabled"] = new_disabled

    final_pos = changes.get("positions", [str(p) for p in lay.get("positions", [])])
    cur_main = str(lay.get("main", final_pos[0] if final_pos else ""))
    new_main = _prompt("Main display", cur_main)
    if new_main != cur_main:
        changes["main"] = new_main

    if changes:
        _update_layout(data, idx, **changes)
        _save_raw_config(config_path, data, ry)
        print("\n  Layout updated.")
    else:
        print("\n  No changes.")


def _setup_delete_layout(config_path: Path, data, ry) -> None:
    """Delete a layout after confirmation."""
    raw_layouts = data.get("layouts", [])
    if not raw_layouts:
        print("\n  No layouts to delete.")
        return

    print()
    for i, lay in enumerate(raw_layouts, 1):
        print(f"    [{i}] {lay.get('name', f'Layout #{i}')}")

    print()
    try:
        pick = input(f"  Select layout to delete [1-{len(raw_layouts)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick.isdigit() or not 1 <= int(pick) <= len(raw_layouts):
        print("  Cancelled.")
        return

    idx = int(pick) - 1
    name = raw_layouts[idx].get("name", f"Layout #{idx + 1}")

    if not _confirm(f"Delete layout '{name}'?"):
        print("  Cancelled.")
        return

    _remove_layout(data, idx)
    _save_raw_config(config_path, data, ry)
    print(f"\n  Layout '{name}' deleted.")


# ---------------------------------------------------------------------------
# Reset disabled displays (reset)
# ---------------------------------------------------------------------------


def reset_main() -> int:
    """Re-enable all disabled-but-connected displays."""
    disabled = _get_disabled_displays()
    if not disabled:
        print("All displays are already active.")
        return 0

    for d, s, v, m, b in disabled:
        kind = "built-in" if b else "external"
        print(f"  Disabled ({kind}): CGDisplayID={d}  serial={s}  vendor=0x{v:x}  model=0x{m:x}")

    ids = [d for d, *_ in disabled]
    print(f"\nRe-enabling {len(ids)} display(s)...")

    if _reenable_displays(ids):
        print("Done.")
        return 0

    print("Error: failed to re-enable displays.", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Manual layout switch (switch)
# ---------------------------------------------------------------------------


def switch_main(config_path: Path) -> int:
    """Show all defined layouts and let user pick one to apply."""
    known_screens, device_set_layouts, _ = load_config(config_path)

    all_layouts: list[Layout] = [
        lay for group in device_set_layouts.values() for lay in group
    ]
    if not all_layouts:
        print("No layouts defined in config.")
        return 1

    output = run_displayplacer_list()
    displays = parse_displays(output)
    displays.extend(_disabled_display_objects())
    hw_map = build_hw_info_map()
    matched, _hw_resolved = match_displays(displays, known_screens, hw_map)

    print("Defined layouts:\n")
    for idx, lay in enumerate(all_layouts, 1):
        tags = []
        if lay.preferred:
            tags.append("preferred")
        print(f"  [{idx}] {lay.name}{'  (' + ', '.join(tags) + ')' if tags else ''}")
        print(f"      match: {', '.join(lay.match)}  enabled: {', '.join(lay.enabled)}")
        if lay.disabled:
            print(f"      disabled: {', '.join(lay.disabled)}")

    print()
    try:
        choice = input(f"Select layout [1-{len(all_layouts)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    if not choice.isdigit():
        return 0

    sel = int(choice) - 1
    if not 0 <= sel < len(all_layouts):
        print("Invalid selection.")
        return 1

    selected = all_layouts[sel]

    if not matched:
        print("No matched displays found -- cannot apply.", file=sys.stderr)
        return 1

    return _apply_layout(selected, matched, known_screens)


def auto_main(config_path: Path) -> int:
    """Non-interactive layout auto-selection. Exits with error if no unambiguous match."""
    known_screens, device_set_layouts, _ = load_config(config_path)
    output = run_displayplacer_list()
    displays = parse_displays(output)
    hw_map = build_hw_info_map()
    matched, hw_resolved = match_displays(displays, known_screens, hw_map)

    if not matched:
        print("No matched displays found.", file=sys.stderr)
        return 1

    sig = tuple(sorted(m.key for m in matched))
    labels = [display_label(m, matched) for m in matched]
    layouts = device_set_layouts.get(sig, [])
    if not hw_resolved:
        layouts = _with_swapped_alternatives(layouts, known_screens)

    if not layouts:
        print(f"No layout for device set: {', '.join(labels)}", file=sys.stderr)
        return 1

    if len(layouts) == 1:
        return _apply_layout(layouts[0], matched, known_screens)

    preferred_lay = next((l for l in layouts if l.preferred), None)
    if preferred_lay:
        return _apply_layout(preferred_lay, matched, known_screens)

    print(
        f"Ambiguous: {len(layouts)} layouts match, none is preferred. "
        "Use 'switch' to choose interactively.",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DisplayPlacer Layout Manager — manage display arrangements using displayplacer.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon", help="run as a persistent daemon, re-applying layout on wake")
    sub.add_parser("install", help="install the LaunchAgent plist and start the daemon")
    sub.add_parser("uninstall", help="stop the daemon and remove the LaunchAgent plist")
    sub.add_parser("start", help="start the daemon via the installed LaunchAgent")
    sub.add_parser("stop", help="stop the daemon (keeps plist; restarts on next login)")
    sub.add_parser("restart", help="stop and start the daemon via the installed LaunchAgent")
    sub.add_parser("status", help="show whether the LaunchAgent is installed and running")
    sub.add_parser("config", help="interactive display and layout configuration editor")
    sub.add_parser("switch", help="list all layouts and interactively select one to apply")
    sub.add_parser("auto", help="auto-apply the preferred layout; abort if ambiguous")
    sub.add_parser("reset", help="re-enable all disabled displays")
    sub.add_parser("init", help="detect connected displays and generate a new config.yml")

    args = parser.parse_args()

    match args.command:
        case "install":
            return install_launch_agent()
        case "uninstall":
            return uninstall_launch_agent()
        case "start":
            return start_launch_agent()
        case "stop":
            return stop_launch_agent()
        case "restart":
            plist = _agent_plist_path()
            if not plist.exists():
                print("LaunchAgent not installed.", file=sys.stderr)
                return 1
            target = f"gui/{_get_uid()}/{_AGENT_LABEL}"
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"Error: {result.stderr.strip()}", file=sys.stderr)
                return 1
            print("Daemon restarted.")
            return 0
        case "status":
            return status_launch_agent()
        case "reset":
            return reset_main()
        case "init":
            return init_main(_CONFIG_PATH)

    if not _CONFIG_PATH.exists():
        print(
            f"Error: config.yml not found (looked in {_CONFIG_PATH.parent}/)\n"
            "Create a config.yml next to display-layout-manager.py to define your "
            "displays and layouts.",
            file=sys.stderr,
        )
        return 1

    match args.command:
        case "config":
            return setup_main(_CONFIG_PATH)
        case "switch":
            return switch_main(_CONFIG_PATH)
        case "auto":
            return auto_main(_CONFIG_PATH)
        case "daemon":
            known_screens, device_set_layouts, options = load_config(_CONFIG_PATH)
            return daemon_main(known_screens, device_set_layouts, options)
        case _:
            known_screens, *_ = load_config(_CONFIG_PATH)
            return show_displays(known_screens)


if __name__ == "__main__":
    raise SystemExit(main())

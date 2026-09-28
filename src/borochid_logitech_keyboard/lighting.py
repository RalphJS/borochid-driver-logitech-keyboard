"""Lighting as HID++ requests. Pure: no I/O.

Two features, both driven only while the host holds lighting control
(RGB_EFFECTS SetSWControl, mode 3):

* **RGB_EFFECTS (0x8071)**, zone effects. fn0 GetInfo describes zones
  (``[zone, 0, location hi, location lo, effect count]``; location 1 is the
  key area, 2 the logo) and each zone's effects (``[zone, index, id hi,
  id lo, ...]``); effects are set by index with fn1 SetEffect
  ``[zone, index, 10 parameter bytes, persistence]``. Persistence 1 means
  "don't store": the broker refuses anything else, so the keyboard's saved
  boot lighting is never touched.
* **PER_KEY_LIGHTING_V2 (0x8081)**, the key colours of the static effect:
  fn6 sets one colour on up to 13 zone ids (``[r, g, b, *ids]``), fn7
  FrameEnd shows the frame. The logo is zone id 210, painted like a key.

Effect IDs and parameter layouts (10 bytes after zone and index), as the
G915 lists them and as Solaar/OpenRGB drive them; periods are big-endian
milliseconds::

    0x00 off      -
    0x01 static   r g b
    0x03 cycle    . . . . . period(2) intensity
    0x04 wave     . . . . . . period-lo direction intensity period-hi
    0x0A breathe  r g b period(2) waveform intensity
    0x0B ripple   r g b . period(2)

The logo has fewer effects (off, static, cycle, breathe); it follows the
keys: wave becomes cycle, ripple becomes a static colour.
"""

from __future__ import annotations

from dataclasses import dataclass

from borochid_logitech_keyboard.settings import Lighting

RGB_EFFECTS = 0x8071
PER_KEY = 0x8081
KEYS_LOCATION, LOGO_LOCATION = 1, 2
LOGO_LED = 210
NOT_PERSISTENT = 0x01
BATCH = 13  # zone ids per SetRgbZonesSingleValue report

OFF, STATIC, CYCLE, WAVE, BREATHE, RIPPLE = 0x00, 0x01, 0x03, 0x04, 0x0A, 0x0B
EFFECT_OF_MODE = {"off": OFF, "per_key": STATIC, "cycle": CYCLE, "wave": WAVE, "breathe": BREATHE, "ripple": RIPPLE}
LOGO_FALLBACK = {WAVE: CYCLE, RIPPLE: STATIC}
FULL_INTENSITY = 100

Request = tuple[int, int, tuple[int, ...]]  # (feature id, function, params)


@dataclass(frozen=True)
class Zone:
    index: int
    location: int
    effects: dict[int, int]  # effect id -> effect index


def parse_zone(info: bytes) -> tuple[int, int]:
    """GetInfo(zone, 0xFF): (location, effect count)."""
    return int.from_bytes(info[2:4], "big"), info[4]


def parse_effect(info: bytes) -> int:
    """GetInfo(zone, index): the effect's id."""
    return int.from_bytes(info[2:4], "big")


def rgb(color: str) -> tuple[int, int, int]:
    v = int(color[1:], 16)
    return v >> 16, (v >> 8) & 0xFF, v & 0xFF


def period(speed: int, slow: int = 20000, fast: int = 1000) -> int:
    """Speed 1 (slowest) to 10 (fastest) as an effect period in ms."""
    return round(slow - (speed - 1) * (slow - fast) / 9)


def effect_params(effect: int, color: str, speed: int) -> tuple[int, ...]:
    r, g, b = rgb(color)
    if effect == STATIC:
        p = [r, g, b]
    elif effect == CYCLE:
        ms = period(speed)
        p = [0, 0, 0, 0, 0, ms >> 8, ms & 0xFF, FULL_INTENSITY]
    elif effect == WAVE:
        ms = period(speed)
        p = [0, 0, 0, 0, 0, 0, ms & 0xFF, 1, FULL_INTENSITY, ms >> 8]  # direction 1: left to right
    elif effect == BREATHE:
        ms = period(speed)
        p = [r, g, b, ms >> 8, ms & 0xFF, 0, FULL_INTENSITY]
    elif effect == RIPPLE:
        ms = period(speed, slow=200, fast=20)
        p = [r, g, b, 0, ms >> 8, ms & 0xFF]
    else:
        p = []
    return tuple(p + [0] * (10 - len(p)))


def set_effect(zone: Zone, effect: int, color: str, speed: int) -> Request | None:
    effect = effect if effect in zone.effects else LOGO_FALLBACK.get(effect, effect)
    if effect not in zone.effects:
        return None
    return (RGB_EFFECTS, 1, (zone.index, zone.effects[effect], *effect_params(effect, color, speed), NOT_PERSISTENT))


OFF_COLOR = "#000000"


def frame(lighting: Lighting, leds: frozenset[int], dark: frozenset[int] = frozenset()) -> list[Request]:
    """``leds`` in one frame: ``color`` everywhere, then the per-key
    overrides, grouped by colour, and ``dark`` keys off; FrameEnd shows it.
    Keys left out keep what they show, so one key can change on its own."""
    by_color: dict[str, list[int]] = {}
    for led in sorted(leds):
        c = OFF_COLOR if led in dark else lighting.keys.get(led, lighting.color)
        by_color.setdefault(c, []).append(led)
    out: list[Request] = []
    for c, ids in by_color.items():
        for i in range(0, len(ids), BATCH):
            out.append((PER_KEY, 6, (*rgb(c), *ids[i : i + BATCH])))
    out.append((PER_KEY, 7, (0, 0, 0, 0, 0)))
    return out


def requests(lighting: Lighting, zones: dict[int, Zone], leds: frozenset[int],
             dark: frozenset[int] = frozenset()) -> list[Request]:
    """Everything that makes the keyboard show ``lighting``; ``dark`` keys
    are off (per-key colours only: effects light every key)."""
    effect = EFFECT_OF_MODE[lighting.mode]
    out: list[Request] = []
    keys, logo = zones.get(KEYS_LOCATION), zones.get(LOGO_LOCATION)
    if lighting.mode == "per_key":
        # Per-key colours are a layer of the static effect.
        for zone in (keys, logo):
            if zone is not None and (req := set_effect(zone, STATIC, lighting.color, lighting.speed)):
                out.append(req)
        return out + frame(lighting, leds, dark)
    if keys is not None and (req := set_effect(keys, effect, lighting.color, lighting.speed)):
        out.append(req)
    if logo is not None and (req := set_effect(logo, effect, lighting.color, lighting.speed)):
        out.append(req)
    return out

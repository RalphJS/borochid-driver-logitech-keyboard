"""The keyboard's settings per Borochid profile, never stored in the keyboard.
Pure: no I/O.

Profiles belong to the service and are shared by every device
(borochid.service.profiles); this module keeps what the keyboard does in
each, keyed by the service's profile id::

    {"report_rate": 1000,              Hz
     "brightness": 100,                0-100, also changed by the brightness key
     "game_mode_keys": [57, 58],       HID usages disabled while game mode is on
     "subprofile": 1,                  the active M-key (1-based)
     "subprofiles": [                  one per M-key
       {"bindings": {"g1": "disabled", "g2": {"keys": ["KEY_LEFTCTRL", "KEY_C"]}},
        "lighting": {"mode": "per_key", "color": "#00b4ff", "speed": 5,
                     "keys": {"38": "#ff0000"}}},
       ...]}

Like G HUB's M-key "subprofiles", M1-M3 switch the G-key bindings and the
lighting within a profile. The game-mode key list, brightness and report
rate belong to the profile.

Lighting modes: ``per_key`` (``color`` on every key, ``keys`` overriding
single keys), ``breathe``, ``cycle``, ``wave``, ``ripple`` (whole-keyboard
effects the keyboard animates itself; ``color`` and ``speed`` 1-10 where
they apply) and ``off``.

Everything read from settings is validated again, so a hand-edited or old
settings file can't put the driver in a bad state: bad fields fall back to
the defaults and are logged.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from borochid.service.host.input import Chord, InputError

log = logging.getLogger(__name__)

DEFAULT_ID = "default"
MODES = ("per_key", "breathe", "cycle", "wave", "ripple", "off")
SPEEDS = range(1, 11)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    leds: frozenset[int]
    usages: frozenset[int]  # keys game mode may disable
    gkeys: frozenset[str]  # keys that take a binding: G-keys, and M-keys/MR for X mode
    subprofiles: int
    rates: tuple[int, ...] = (1000, 500, 250, 125)
    xkeys: frozenset[str] = frozenset()  # M-keys and MR: X mode's macro keys, with a white LED each

    def rate(self, value: Any) -> int:
        if value not in self.rates or isinstance(value, bool):
            raise SettingsError(f"report rate must be one of {', '.join(map(str, self.rates))} Hz")
        return int(value)

    def subprofile(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.subprofiles:
            raise SettingsError(f"subprofile must be 1-{self.subprofiles}")
        return value

    def game_mode_keys(self, value: Any) -> list[int]:
        if not isinstance(value, list) or any(isinstance(u, bool) or not isinstance(u, int) for u in value):
            raise SettingsError("game mode keys must be a list of key usages")
        if bad := sorted(set(value) - self.usages):
            raise SettingsError(f"not a key game mode can disable: {', '.join(map(str, bad))}")
        return sorted(set(value))

    def x_lights(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
            raise SettingsError("X mode lights must be a list of key ids")
        if bad := sorted(set(value) - self.xkeys):
            raise SettingsError(f"no light to turn on in X mode: {', '.join(bad)}")
        return sorted(set(value))

    def led(self, value: Any) -> int:
        try:
            led = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"not a key: {value!r}") from None
        if isinstance(value, bool) or led not in self.leds:
            raise SettingsError(f"key {value!r} has no light")
        return led


def color(value: Any) -> str:
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        raise SettingsError("a colour is '#rrggbb'")
    return value.lower()


def brightness(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise SettingsError("brightness must be 0-100")
    return value


def switch(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SettingsError("must be true or false")
    return value


def binding(value: Any) -> Any:
    """What a G-key does: ``"disabled"`` or a chord (keys or a wheel step)
    replayed through the service's virtual input device."""
    if value == "disabled":
        return value
    try:
        return Chord.parse(value).to_json()
    except InputError as e:
        raise SettingsError(str(e)) from None


@dataclass
class Lighting:
    mode: str = "per_key"
    color: str = "#00b4ff"
    speed: int = 5
    keys: dict[int, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode, "color": self.color, "speed": self.speed, "keys": {str(k): c for k, c in sorted(self.keys.items())}}

    @classmethod
    def parse(cls, raw: Any, default: Lighting, limits: Limits, strict: bool) -> Lighting:
        raw = raw if isinstance(raw, dict) else {}
        out = copy.deepcopy(default)
        checks = {"mode": mode, "color": color, "speed": speed}
        for key, fn in checks.items():
            if key in raw:
                try:
                    setattr(out, key, fn(raw[key]))
                except SettingsError as e:
                    if strict:
                        raise SettingsError(f"lighting.{key}: {e}") from None
                    log.warning("ignoring stored lighting %s=%r: %s", key, raw[key], e)
        keys = raw.get("keys", {})
        if isinstance(keys, dict):
            out.keys = {}
            for k, c in keys.items():
                try:
                    out.keys[limits.led(k)] = color(c)
                except SettingsError as e:
                    if strict:
                        raise
                    log.warning("ignoring stored key colour %s=%r: %s", k, c, e)
        return out


def mode(value: Any) -> str:
    if value not in MODES:
        raise SettingsError(f"lighting mode must be one of {', '.join(MODES)}")
    return value


def speed(value: Any) -> int:
    if isinstance(value, bool) or value not in SPEEDS:
        raise SettingsError("speed must be 1-10")
    return value


@dataclass
class Subprofile:
    bindings: dict[str, Any] = field(default_factory=dict)
    lighting: Lighting = field(default_factory=Lighting)

    def to_json(self) -> dict[str, Any]:
        return {"bindings": copy.deepcopy(self.bindings), "lighting": self.lighting.to_json()}


@dataclass
class Settings:
    report_rate: int = 1000
    brightness: int = 100
    game_mode_keys: list[int] = field(default_factory=list)
    subprofile: int = 1
    subprofiles: list[Subprofile] = field(default_factory=list)
    # X mode: no subprofiles (M1's lighting and bindings), and M1-M3/MR
    # replay their own bindings like G-keys.
    x_mode: bool = False
    # Which M-key/MR LEDs are lit in X mode (they are white, on or off).
    x_lights: list[str] = field(default_factory=list)
    # Num Lock status: the Num Lock key lit only while Num Lock is on.
    numlock_light: bool = False

    @property
    def sub(self) -> Subprofile:
        """The subprofile in use: M1's in X mode, whatever is selected."""
        return self.subprofiles[0 if self.x_mode else self.subprofile - 1]

    def to_json(self) -> dict[str, Any]:
        return {
            "report_rate": self.report_rate,
            "brightness": self.brightness,
            "game_mode_keys": list(self.game_mode_keys),
            "subprofile": self.subprofile,
            "subprofiles": [s.to_json() for s in self.subprofiles],
            "x_mode": self.x_mode,
            "x_lights": list(self.x_lights),
            "numlock_light": self.numlock_light,
        }

    @classmethod
    def defaults(cls, raw: dict[str, Any], limits: Limits) -> Settings:
        """The package's starting settings; strict, so a broken package is
        reported instead of silently patched."""
        if not isinstance(raw, dict):
            raise SettingsError("defaults must be an object")
        bindings = raw.get("bindings", {})
        if not isinstance(bindings, dict) or set(bindings) - limits.gkeys:
            raise SettingsError("bindings may only name the package's G-keys")
        sub = Subprofile(
            bindings={g: binding(bindings.get(g, "disabled")) for g in sorted(limits.gkeys)},
            lighting=Lighting.parse(raw.get("lighting", {}), Lighting(), limits, strict=True),
        )
        return cls(
            report_rate=limits.rate(raw.get("report_rate", 1000)),
            brightness=brightness(raw.get("brightness", 100)),
            game_mode_keys=limits.game_mode_keys(raw.get("game_mode_keys", [])),
            subprofile=1,
            subprofiles=[copy.deepcopy(sub) for _ in range(limits.subprofiles)],
            x_mode=switch(raw.get("x_mode", False)),
            x_lights=limits.x_lights(raw.get("x_lights", sorted(limits.xkeys))),  # all lit to start
            numlock_light=switch(raw.get("numlock_light", False)),
        )

    @classmethod
    def parse(cls, raw: Any, defaults: Settings, limits: Limits) -> Settings:
        """Tolerant: each bad field falls back to the default."""
        raw = raw if isinstance(raw, dict) else {}
        s = copy.deepcopy(defaults)

        def take(key: str, fn):
            if key in raw:
                try:
                    return fn(raw[key])
                except (SettingsError, TypeError, ValueError) as e:
                    log.warning("ignoring stored %s=%r: %s", key, raw[key], e)
            return getattr(s, key)

        s.report_rate = take("report_rate", limits.rate)
        s.brightness = take("brightness", brightness)
        s.game_mode_keys = take("game_mode_keys", lambda v: limits.game_mode_keys(_known_usages(v, limits)))
        s.subprofile = take("subprofile", limits.subprofile)
        s.x_mode = take("x_mode", switch)
        s.x_lights = take("x_lights", limits.x_lights)
        s.numlock_light = take("numlock_light", switch)
        stored = raw.get("subprofiles")
        for i, sub in enumerate(stored[: limits.subprofiles] if isinstance(stored, list) else []):
            if not isinstance(sub, dict):
                continue
            target = s.subprofiles[i]
            for g, value in (sub.get("bindings") or {}).items() if isinstance(sub.get("bindings"), dict) else ():
                if g not in limits.gkeys:
                    continue
                try:
                    target.bindings[g] = binding(value)
                except SettingsError as e:
                    log.warning("ignoring stored binding %s=%r: %s", g, value, e)
            target.lighting = Lighting.parse(sub.get("lighting"), target.lighting, limits, strict=False)
        return s


def _known_usages(value: Any, limits: Limits) -> Any:
    """A stored game-mode list without keys the package no longer lets it
    hold (e.g. since locked: the firmware blocks them anyway), so a package
    update doesn't throw away the rest of the list."""
    if not isinstance(value, list):
        return value
    kept = [u for u in value if u in limits.usages and not isinstance(u, bool)]
    if dropped := [u for u in value if u not in kept]:
        log.info("dropping stored game mode keys %s: not keys it can disable any more", dropped)
    return kept


class Profiles:
    """Settings per profile id, and which one is in use, as stored::

        {"profiles": {"default": {...}, "p3f9a1c2e": {...}}}
    """

    def __init__(self, defaults: Settings, limits: Limits):
        self.defaults = defaults
        self.limits = limits
        self.items: dict[str, Settings] = {DEFAULT_ID: copy.deepcopy(defaults)}
        self.active = DEFAULT_ID

    def load(self, stored: dict[str, Any]) -> None:
        raw = stored.get("profiles")
        if isinstance(raw, dict):
            items = {k: Settings.parse(v, self.defaults, self.limits) for k, v in raw.items() if isinstance(k, str)}
            self.items = items or {DEFAULT_ID: copy.deepcopy(self.defaults)}
        if self.active not in self.items:
            self.items[self.active] = copy.deepcopy(self.defaults)

    def dump(self) -> dict[str, Any]:
        return {"profiles": {k: s.to_json() for k, s in self.items.items()}}

    @property
    def current(self) -> Settings:
        return self.items[self.active]

    def use(self, pid: str, copy_of: str | None = None, known: set[str] | None = None) -> None:
        """Switch to ``pid``, creating it from ``copy_of`` (or the defaults)
        the first time; drop profiles not in ``known``."""
        if pid not in self.items:
            source = self.items.get(copy_of) if copy_of else None
            self.items[pid] = copy.deepcopy(source or self.defaults)
        self.active = pid
        if known is not None:
            for stale in set(self.items) - set(known) - {pid}:
                del self.items[stale]

"""The device package's description of a keyboard, validated. Holds what the
protocol can't report: which keys exist, their lighting zone ids and HID
usages, the G-keys, and the starting settings.

    "keyboard": {"keys": [                       drawn by the GUI, checked here
      {"id": "esc", "label": "Esc", "x": 1.25, "y": 1, "led": 38, "usage": 41},
      ...]},
    "hidpp_keyboard": {
      "gkeys": [{"id": "g1", "label": "G1", "bit": 1}, ...],
      "subprofiles": 3,                          M1-M3
      "game_mode_locked": [227],                 always blocked by the firmware
      "defaults": {"report_rate": 1000, "brightness": 100, "game_mode_keys": [],
                   "bindings": {"g1": "disabled"},
                   "lighting": {"mode": "per_key", "color": "#00b4ff", "speed": 5, "keys": {}}}
    }

``bit`` is the G-key's position in GKEY notifications (1 = bit 0). ``led``
is a PER_KEY_LIGHTING_V2 zone id; keys without one have no RGB LED.
``usage`` is the HID keyboard usage DISABLE_KEYS_BY_USAGE takes; keys
without one can't be disabled in game mode.

With subprofiles, M1-Mn (``m1``...) and MR (``mr``) can take bindings too:
X mode turns subprofiles off and makes them macro keys like the G-keys.
The key with usage 83 (Num Lock) is the one Num Lock status darkens.

Validate a package with ``python -m borochid_logitech_keyboard.model MANIFEST``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from borochid_logitech_keyboard import settings

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
MAX_KEYS = 256
MAX_GKEYS = 32
MAX_SUBPROFILES = 8  # MKEYS reports up to 8 M-keys
NUM_LOCK_USAGE = 83


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class GKey:
    id: str
    label: str
    bit: int

    @property
    def mask(self) -> int:
        return 1 << (self.bit - 1)


@dataclass(frozen=True)
class Model:
    gkeys: tuple[GKey, ...]
    leds: frozenset[int]
    usages: frozenset[int]
    subprofiles: int
    game_mode_locked: frozenset[int]
    defaults: settings.Settings
    reply_timeout_s: float = 1.0
    numlock_led: int | None = None

    @property
    def mkeys(self) -> tuple[GKey, ...]:
        """M1-Mn as X mode's macro keys; ``bit`` is their MKEYS bit."""
        return tuple(GKey(f"m{n}", f"M{n}", n) for n in range(1, self.subprofiles + 1)) if self.subprofiles > 1 else ()

    @property
    def mr(self) -> GKey | None:
        """MR as X mode's macro key; MR notifies 1 (pressed) or 0."""
        return GKey("mr", "MR", 1) if self.subprofiles > 1 else None

    @property
    def bindable(self) -> tuple[GKey, ...]:
        """Every key that takes a binding: the G-keys, then M-keys and MR."""
        return self.gkeys + self.mkeys + ((self.mr,) if self.mr else ())

    @property
    def limits(self) -> settings.Limits:
        xkeys = frozenset(g.id for g in self.mkeys + ((self.mr,) if self.mr else ()))
        return settings.Limits(self.leds, self.usages - self.game_mode_locked, frozenset(g.id for g in self.bindable),
                               self.subprofiles, xkeys=xkeys)

    def gkey(self, key_id: Any) -> GKey:
        """A key that takes a binding (G-key, or M-key/MR for X mode)."""
        for g in self.bindable:
            if g.id == key_id:
                return g
        raise ModelError(f"no G-key {key_id!r}")

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> Model:
        spec = manifest.get("hidpp_keyboard")
        if not isinstance(spec, dict):
            raise ModelError("manifest needs a 'hidpp_keyboard' section")
        keys = (manifest.get("keyboard") or {}).get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
            raise ModelError(f"keyboard.keys must list 1-{MAX_KEYS} keys")
        leds, usages, ids = set(), set(), set()
        numlock_led = None
        for k in keys:
            if not isinstance(k, dict) or not _ID_RE.match(str(k.get("id", ""))):
                raise ModelError(f"key id must match {_ID_RE.pattern}: {k!r}")
            if k["id"] in ids:
                raise ModelError(f"key {k['id']} listed twice")
            ids.add(k["id"])
            for field, seen, hi in (("led", leds, 255), ("usage", usages, 255)):
                if field in k:
                    v = k[field]
                    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= hi:
                        raise ModelError(f"key {k['id']}: {field} must be 1-{hi}")
                    if v in seen:
                        raise ModelError(f"key {k['id']}: {field} {v} used twice")
                    seen.add(v)
            if k.get("usage") == NUM_LOCK_USAGE:
                numlock_led = k.get("led")

        gkeys = []
        for g in spec.get("gkeys", []):
            if not isinstance(g, dict) or not _ID_RE.match(str(g.get("id", ""))):
                raise ModelError(f"G-key id must match {_ID_RE.pattern}: {g!r}")
            bit = g.get("bit")
            if isinstance(bit, bool) or not isinstance(bit, int) or not 1 <= bit <= MAX_GKEYS:
                raise ModelError(f"G-key {g['id']}: bit must be 1-{MAX_GKEYS}")
            gkeys.append(GKey(g["id"], str(g.get("label", g["id"])), bit))
        if len({g.id for g in gkeys}) != len(gkeys) or len({g.bit for g in gkeys}) != len(gkeys):
            raise ModelError("G-key ids and bits must be unique")

        subs = spec.get("subprofiles", 1)
        if isinstance(subs, bool) or not isinstance(subs, int) or not 1 <= subs <= MAX_SUBPROFILES:
            raise ModelError(f"subprofiles must be 1-{MAX_SUBPROFILES}")
        locked = spec.get("game_mode_locked", [])
        if not isinstance(locked, list) or not all(isinstance(u, int) and not isinstance(u, bool) for u in locked):
            raise ModelError("game_mode_locked must list HID usages")

        # Limits need the model's bindable keys, which need the model: build
        # it with placeholder defaults, then the real ones.
        timing = {"reply_timeout_s": float(spec["reply_timeout_s"])} if "reply_timeout_s" in spec else {}
        model = cls(tuple(gkeys), frozenset(leds), frozenset(usages), subs, frozenset(locked), settings.Settings(),
                    numlock_led=numlock_led, **timing)
        try:
            defaults = settings.Settings.defaults(spec.get("defaults", {}), model.limits)
        except (settings.SettingsError, AttributeError, TypeError) as e:
            raise ModelError(f"hidpp_keyboard.defaults: {e}") from None
        return dataclasses.replace(model, defaults=defaults)


def main() -> None:
    for arg in sys.argv[1:]:
        try:
            m = Model.from_manifest(json.loads(Path(arg).read_text()))
        except (OSError, ValueError) as e:
            sys.exit(f"{arg}: {e}")
        print(f"{arg}: {len(m.leds)} LEDs, {len(m.usages)} keys, {len(m.gkeys)} G-keys, {m.subprofiles} subprofiles: OK")


if __name__ == "__main__":
    main()

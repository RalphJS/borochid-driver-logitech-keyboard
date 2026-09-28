"""Borochid driver for Logitech keyboards that speak HID++ 2.0 (first model:
G915 WIRELESS), reached through the HID++ broker (see ``channel.py`` and
``broker/``).

While the service runs the keyboard is in **host mode**: its onboard
profiles stop, and this driver applies its settings for the active Borochid
profile (see ``settings.py``). Nothing is ever written to the keyboard's
memory; everything below is RAM state that a power cycle clears.

* **G1-G5** are diverted (GKEY): the keyboard reports them only to this
  driver, which replays the bound chord through the service's virtual
  input device while the key is held.
* **M1-M3** pick the profile's subprofile (bindings and lighting); their
  LEDs show which. **MR** is diverted and does nothing: recording a macro
  would mean reading keystrokes, which Borochid never does.
* **X mode** (per profile) turns subprofiles off: M1's bindings and
  lighting apply, and M1-M3 and MR replay their own bindings like G-keys.
  Their white LEDs are on or off as the profile says (``x_lights``).
* **Num Lock status** (per profile, per-key colours only): the Num Lock key
  is dark while Num Lock is off. The state is the keyboard's own Num Lock
  LED as the kernel sets it (sysfs), never a keystroke.
* **Lighting** is the host's (RGB_EFFECTS SetSWControl): per-key colours or
  a whole-keyboard effect, see ``lighting.py``.
* **Game mode** stays the keyboard's: its key toggles it and lights up.
  The profile decides which keys it disables (DISABLE_KEYS_BY_USAGE); the
  firmware always disables both Super keys on its own.
* **Brightness**: the brightness key steps it in the keyboard, which tells
  the driver; it is saved with the profile.
* **Battery**: from the cell voltage (``battery.py``), polled and notified.

Measured on a G915 through its LIGHTSPEED receiver (046d:c541):

* The receiver forwards no notifications unless the host enables them (the
  broker does). Link changes arrive as HID++ 1.0 ``0x41`` reports from the
  receiver, bit 6 of byte 4 meaning "no link".
* A sleeping keyboard answers nothing; the next report from it (or a link
  report) wakes the driver, which sets everything up again. Host mode
  doesn't survive a power cycle, so setting up again is always safe.

Link states, published as ``link``: ``connecting`` (no broker yet),
``online``, ``asleep``, ``error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from pathlib import Path
from typing import Any

from borochid.common.models import Bus
from borochid.service.drivers import Driver, DriverError
from borochid.service.host.input import Chord, InputError
from borochid.service.profiles import Profile as SharedProfile

from borochid_logitech_hidpp import protocol
from borochid_logitech_hidpp.protocol import Message, u16
from borochid_logitech_hidpp.session import HidppError, NoReply, Session, Unsupported

from borochid_logitech_keyboard import battery, lighting, settings
from borochid_logitech_keyboard.channel import NoLink
from borochid_logitech_keyboard.model import Model, ModelError

log = logging.getLogger(__name__)

DEVICE_INFO = 0x0003  # DEVICE_FW_VERSION: fn0 holds the unit ID
WIRELESS_DEVICE_STATUS = 0x1D4B
BATTERY_VOLTAGE = 0x1001
DISABLE_KEYS = 0x4522
GKEY, MKEYS, MR = 0x8010, 0x8020, 0x8030
BRIGHTNESS = 0x8040
REPORT_RATE = 0x8060
ONBOARD_PROFILES = 0x8100
ONBOARD_MODE, HOST_MODE = 1, 2
SW_CONTROL_TAKE = (1, 3, 4)
SW_CONTROL_RELEASE = (1, 0, 0)
USAGES_PER_CALL = 16
LINK_REPORTS = (0x40, 0x41)  # HID++ 1.0 receiver notifications: disconnect, connect
BATTERY_POLL_S = 300
NUMLOCK_POLL_S = 0.25  # sysfs has no change events for an input LED
NUMLOCK_FIND_S = 2.0  # how often to look for the LED again while it's missing
HID_SYSFS = Path("/sys/bus/hid/devices")


def _read_lower(path: Path) -> str:
    try:
        return path.read_text().lower()
    except OSError:
        return ""

Quiet = (NoReply, NoLink)


class KeyboardDriver(Driver):
    supports_profiles = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.model = Model.from_manifest(self.manifest.raw)
        except ModelError as e:
            raise DriverError(f"{self.manifest.id}: {e}") from e
        self.session = Session(self._write, self.model.reply_timeout_s)
        self.profiles = settings.Profiles(self.model.defaults, self.model.limits)
        self.profiles.load(self.settings)
        self.profile_name = "Default"
        self._shared: tuple[str, str | None, set[str]] | None = None  # the last use_profile()
        self.zones: dict[int, lighting.Zone] = {}
        self._chords: dict[str, Chord] = {}
        self._gmask = 0
        self._mmask = 0
        self._mrmask = 0
        self._numlock_path = self._find_numlock()
        self._numlock: bool | None = None
        self._has_rate = True  # no REPORT_RATE over Bluetooth: the link sets the rate
        self._apply_lock = asyncio.Lock()  # one frame at a time: a Num Lock change can't split one
        self._ready = False
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        if hasattr(self.channel, "on_link"):
            self.channel.on_link = self._on_link

        self.state = {
            "link": "connecting",
            "status": "Connecting",
            "online": False,
            "battery": None,
            "charging": None,
            "input_error": None,
        }
        self.state.update(self._profile_state())
        self._resolve_chords()

    # -- state ---------------------------------------------------------------------

    def _profile_state(self) -> dict[str, Any]:
        s = self.profiles.current
        light = s.sub.lighting
        out: dict[str, Any] = {
            "profile": self.profile_name,  # the shared profile's name, without the subprofile
            "report_rate": s.report_rate,
            "brightness": s.brightness,
            "game_mode_keys": list(s.game_mode_keys),
            "subprofile": s.subprofile,
            "x_mode": s.x_mode,
            "numlock_light": s.numlock_light,
            **{f"x_light.{k}": k in s.x_lights for k in sorted(self.model.limits.xkeys)},
            "lighting.mode": light.mode,
            "lighting.color": light.color,
            "lighting.speed": light.speed,
            "lighting.keys": {str(k): c for k, c in sorted(light.keys.items())},
            "lighting.per_key": light.mode == "per_key",
            "lighting.uses_color": light.mode in ("per_key", "breathe", "ripple"),
            "lighting.uses_speed": light.mode in ("breathe", "cycle", "wave", "ripple"),
        }
        for g in self.model.bindable:
            out[f"bind.{g.id}"] = s.sub.bindings.get(g.id, "disabled")
        return out

    def _status(self) -> str:
        link = self.state.get("link")
        if link == "asleep":
            return "Asleep"
        if link == "connecting":
            return getattr(self.channel, "problem", None) or "Connecting"
        if self.profiles.current.x_mode:
            return f"{self.profile_name} · X mode"
        if self.model.subprofiles > 1:
            return f"{self.profile_name} · M{self.profiles.current.subprofile}"
        return self.profile_name

    def _save(self) -> None:
        self.settings.update(self.profiles.dump())
        self.save_settings()

    def _resolve_chords(self) -> None:
        self._chords = {}
        for g, value in self.profiles.current.sub.bindings.items():
            if value != "disabled":
                self._chords[g] = Chord.parse(value)

    # -- lifecycle -----------------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        await self.channel.write(data)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            if isinstance(exc, Quiet):
                self._went_quiet(exc)
            else:
                log.error("%s: background task failed: %s", self.channel.ident.uid, exc, exc_info=exc)

    async def start(self) -> None:
        # Bring-up doesn't wait for the keyboard: it may be asleep, or the
        # broker not running yet.
        self.spawn(self._run())
        self.spawn(self._poll_battery())
        if self.model.numlock_led is not None:
            self.spawn(self._watch_numlock())

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            if getattr(self.channel, "up", True):
                # A receiver knows its keyboard's unit ID even while the
                # keyboard is away (on its cable, say): identify right away.
                if unit := getattr(self.channel, "unit_id", None):
                    await self.identify(unit)
                try:
                    await self._setup()
                except Quiet as e:
                    self._went_quiet(e)
                except (HidppError, Unsupported) as e:
                    log.error("%s: cannot set up the keyboard: %s", self.channel.ident.uid, e)
                    self.publish({"link": "error", "status": f"Error: {e}"})
            await self._wake.wait()

    def _on_link(self, up: bool, problem: str | None) -> None:
        if up:
            self._wake.set()
            return
        self._ready = False
        self._release_all()
        self.publish({"link": "connecting", "online": False})
        self.publish({"status": self._status()})

    def _went_quiet(self, why: Exception | None = None) -> None:
        if self.state.get("link") == "connecting":
            return  # no broker: nothing to say about the keyboard
        if why is not None and self.state.get("link") != "asleep":
            log.info("%s: keyboard stopped answering: %s", self.channel.ident.uid, why)
        self._ready = False
        self._release_all()
        self.publish({"link": "asleep", "online": False, "status": "Asleep"})

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        if self._ready:
            with contextlib.suppress(*Quiet, HidppError, Unsupported, OSError):
                await self._leave_host()
        self._ready = False
        self._release_all()
        if self.host and self.host.input:
            self.host.input.close()

    # -- setup -----------------------------------------------------------------------

    async def _setup(self) -> None:
        log.debug("%s: setting up the keyboard", self.channel.ident.uid)
        self.session.forget()
        s = self.session
        # First, so the keyboard's own settings are the ones applied below.
        if unit := await self._read_unit_id():
            await self.identify(unit)
        try:
            mask = (await s.feature(REPORT_RATE, 0))[0]
            self._has_rate = True
        except Unsupported:
            mask, self._has_rate = 0, False  # kept in the profile for the other connections
        self.publish({"report_rate_available": self._has_rate})
        rates = tuple(sorted({1000 // (n + 1) for n in range(8) if mask >> n & 1}, reverse=True))
        if rates:
            self.profiles.limits = settings.Limits(**{**self.model.limits.__dict__, "rates": rates})
        if not self.zones:
            self.zones = await self._read_zones()
        for feature in (WIRELESS_DEVICE_STATUS, BATTERY_VOLTAGE, MKEYS, MR, BRIGHTNESS):
            await s.has(feature)  # cache the index, to recognise its notifications
        await s.feature(ONBOARD_PROFILES, 1, HOST_MODE)
        await s.feature(lighting.RGB_EFFECTS, 5, *SW_CONTROL_TAKE)
        await s.feature(GKEY, 2, 1)
        self._gmask = self._mmask = 0
        await self._apply("all")
        self._ready = True
        self.publish({"link": "online", "online": True})
        self.publish({"status": self._status()})
        await self._read_battery()
        log.info("%s: online, profile %r", self.channel.ident.uid, self.profile_name)

    async def _read_unit_id(self) -> str | None:
        """The keyboard's unit ID (DEVICE_FW_VERSION getDeviceInfo): the same
        through the receiver and on the cable, unlike the USB serial or port."""
        try:
            info = await self.session.feature(DEVICE_INFO, 0)
        except (HidppError, Unsupported):
            return None
        unit = bytes(info[1:5])
        return unit.hex().upper() if any(unit) else None

    async def settings_reloaded(self) -> None:
        """The keyboard's own settings were found (or its other connection
        changed them): rebuild the profiles and, if it listens, apply them."""
        limits = self.profiles.limits
        self.profiles = settings.Profiles(self.model.defaults, limits)
        self.profiles.load(self.settings)
        if self._shared is not None:
            self.profiles.use(*self._shared)
        self._release_all()
        self._resolve_chords()
        self.publish({**self._profile_state(), "status": self._status()})
        if self._ready:
            try:
                await self._apply("all")
            except Quiet as e:
                self._went_quiet(e)

    async def _read_zones(self) -> dict[int, lighting.Zone]:
        """RGB_EFFECTS zones by location, with their effect ids."""
        s = self.session
        zones: dict[int, lighting.Zone] = {}
        count = (await s.feature(lighting.RGB_EFFECTS, 0, 0xFF, 0xFF, 0))[2]
        for z in range(count):
            location, n = lighting.parse_zone(await s.feature(lighting.RGB_EFFECTS, 0, z, 0xFF, 0))
            effects = {}
            for e in range(n):
                effects[lighting.parse_effect(await s.feature(lighting.RGB_EFFECTS, 0, z, e, 0))] = e
            zones[location] = lighting.Zone(z, location, effects)
        return zones

    async def _leave_host(self) -> None:
        s = self.session
        self._release_all()
        for feature, fn, params in (
            (GKEY, 2, (0,)),
            (MKEYS, 1, (0,)),
            (lighting.RGB_EFFECTS, 5, SW_CONTROL_RELEASE),
            (DISABLE_KEYS, 3, ()),
        ):
            with contextlib.suppress(HidppError, Unsupported):
                await s.feature(feature, fn, *params)
        await s.feature(ONBOARD_PROFILES, 1, ONBOARD_MODE)

    async def _apply(self, what: str) -> None:
        """Push part of the active profile to the keyboard: "all", "rate",
        "brightness", "game_mode", "subprofile" (M LED, lighting, bindings),
        "lighting", "numlock" (the Num Lock key alone) or "bindings" (host
        side only: the input device)."""
        async with self._apply_lock:
            await self._apply_now(what)

    async def _apply_now(self, what: str) -> None:
        s, cur = self.session, self.profiles.current
        if what in ("all", "rate") and self._has_rate:
            await s.feature(REPORT_RATE, 2, 1000 // cur.report_rate)
        if what in ("all", "brightness"):
            await s.feature(BRIGHTNESS, 2, cur.brightness >> 8, cur.brightness & 0xFF)
        if what in ("all", "game_mode"):
            await s.feature(DISABLE_KEYS, 3)
            keys = cur.game_mode_keys
            for i in range(0, len(keys), USAGES_PER_CALL):
                await s.feature(DISABLE_KEYS, 1, *keys[i : i + USAGES_PER_CALL])
        if what in ("all", "subprofile"):
            # X mode: the M/MR LEDs the profile lights; otherwise the subprofile's M LED.
            if cur.x_mode:
                lit = set(cur.x_lights)
                await s.feature(MKEYS, 1, sum(g.mask for g in self.model.mkeys if g.id in lit))
                await s.feature(MR, 0, 1 if "mr" in lit else 0)
            else:
                await s.feature(MKEYS, 1, 1 << (cur.subprofile - 1))
                await s.feature(MR, 0, 0)
        if what in ("all", "subprofile", "lighting"):
            for feature, fn, params in lighting.requests(cur.sub.lighting, self.zones, self.model.leds, self._dark()):
                await s.feature(feature, fn, *params)
        elif what == "numlock" and cur.sub.lighting.mode == "per_key" and self.model.numlock_led is not None:
            # Just the Num Lock key; the rest keeps what it shows.
            for feature, fn, params in lighting.frame(cur.sub.lighting, frozenset({self.model.numlock_led}), self._dark()):
                await s.feature(feature, fn, *params)
        if what in ("all", "subprofile", "bindings"):
            await self._open_input()

    async def _open_input(self) -> None:
        if not self._chords:
            return
        if not (self.host and self.host.input):
            self.publish({"input_error": "this package does not enable the input service"})
            return
        try:
            await self.host.input.open()
            self.publish({"input_error": None})
        except InputError as e:
            log.warning("%s: %s", self.channel.ident.uid, e)
            self.publish({"input_error": str(e)})

    async def _read_battery(self) -> None:
        try:
            reading = battery.parse(await self.session.feature(BATTERY_VOLTAGE, 0))
        except Unsupported:
            return
        self._battery(reading)

    def _battery(self, reading: battery.Reading) -> None:
        self.publish({"battery": reading.level, "charging": reading.charging})

    def _find_numlock(self) -> Path | None:
        """The Num Lock LED the kernel keeps for this keyboard: under its USB
        device (receiver or cable), or its Bluetooth HID device."""
        if self.model.numlock_led is None:
            return None
        ident = self.channel.ident
        if address := ident.attrs.get("address") if ident.bus is Bus.BLE else None:
            for hid in sorted(HID_SYSFS.glob("0005:*")):
                if f"hid_uniq={str(address).lower()}" in _read_lower(hid / "uevent").splitlines():
                    found = sorted(hid.glob("input/input*/input*::numlock/brightness"))
                    return found[0] if found else None
            return None
        sys_path = ident.attrs.get("sys_path")
        if not sys_path:
            return None
        found = sorted(Path(sys_path).glob("*/*/input/input*/input*::numlock/brightness"))
        return found[0] if found else None

    def _dark(self) -> frozenset[int]:
        """Keys to show unlit: the Num Lock key while Num Lock is off, if asked."""
        if self.profiles.current.numlock_light and self._numlock is False and self.model.numlock_led is not None:
            return frozenset({self.model.numlock_led})
        return frozenset()

    async def _watch_numlock(self) -> None:
        next_look = 0.0
        while True:
            # The LED moves: a Bluetooth reconnect (or a replug) makes a new
            # input device, and over Bluetooth there's none while away.
            if self._numlock_path is None and time.monotonic() >= next_look:
                self._numlock_path = self._find_numlock()
                next_look = time.monotonic() + NUMLOCK_FIND_S
                if self._numlock_path is not None:
                    log.debug("%s: Num Lock LED at %s", self.channel.ident.uid, self._numlock_path)
            on: bool | None = None
            if self._numlock_path is not None:
                try:
                    on = self._numlock_path.read_text().strip() not in ("", "0")
                except OSError:
                    self._numlock_path = None  # gone: look for it again
            if on != self._numlock:
                log.debug("%s: Num Lock %s", self.channel.ident.uid, {True: "on", False: "off", None: "unknown"}[on])
                self._numlock = on
                if self._ready and self.profiles.current.numlock_light:
                    try:
                        await self._apply("numlock")
                    except Quiet as e:
                        self._went_quiet(e)
                    except (HidppError, Unsupported) as e:
                        log.warning("%s: can't show Num Lock: %s", self.channel.ident.uid, e)
            await asyncio.sleep(NUMLOCK_POLL_S)

    async def _poll_battery(self) -> None:
        while True:
            await asyncio.sleep(BATTERY_POLL_S)
            if self._ready:
                with contextlib.suppress(HidppError):
                    await self._read_battery()

    # -- input from the keyboard -------------------------------------------------

    def on_data(self, data: bytes) -> None:
        if len(data) >= 4 and data[0] == protocol.SHORT and data[2] in LINK_REPORTS:
            self._link_report(data)
            return
        msg = protocol.parse(data)
        if msg is None:
            return
        if msg.sw_id == protocol.SW_ID:
            self.session.feed(msg)
        elif msg.sw_id == 0 and isinstance(msg, Message):
            if self.state.get("link") == "asleep":
                self._wake.set()  # it's back: set everything up again
            self._notification(msg)

    def _link_report(self, data: bytes) -> None:
        # [0x10, index, 0x41, protocol, flags, pid lo, pid hi]; flags bit 6: no link.
        lost = data[2] == 0x40 or (len(data) > 4 and bool(data[4] & 0x40))
        if lost:
            log.info("%s: keyboard link lost", self.channel.ident.uid)
            self._went_quiet()
        else:
            # Power-on or reconnect: host mode is gone, set up again.
            log.info("%s: keyboard connected", self.channel.ident.uid)
            self._ready = False
            self._wake.set()

    def _notification(self, msg: Message) -> None:
        index = self.session.index
        fi = msg.feature_index
        if fi == index.get(GKEY) and msg.function == 0:
            self._gkeys(int.from_bytes(msg.params[0:4], "little"))
        elif fi == index.get(MKEYS) and msg.function == 0:
            self._mkeys(msg.params[0])
        elif fi == index.get(MR) and msg.function == 0:
            self._mr(msg.params[0])
        elif fi == index.get(BRIGHTNESS) and msg.function == 0:
            self._brightness_changed(u16(msg.params, 0))
        elif fi == index.get(BATTERY_VOLTAGE) and msg.function == 0:
            self._battery(battery.parse(msg.params))
        elif fi == index.get(WIRELESS_DEVICE_STATUS):
            log.info("%s: keyboard reconnected", self.channel.ident.uid)
            self._ready = False
            self._wake.set()
        # MR presses (and anything else) are ignored.

    def _replay(self, keys: tuple, mask: int, old: int) -> None:
        """Hold each key's chord while its bit is set in ``mask``."""
        changed = mask ^ old
        inp = self.host.input if self.host else None
        for g in keys:
            if not changed & g.mask or (chord := self._chords.get(g.id)) is None or inp is None:
                continue
            if mask & g.mask:
                inp.down(g.id, chord)
            else:
                inp.up(g.id)

    def _gkeys(self, mask: int) -> None:
        if not self._ready:
            return
        old, self._gmask = self._gmask, mask
        self._replay(self.model.gkeys, mask, old)

    def _mr(self, pressed: int) -> None:
        if not self._ready or not self.profiles.current.x_mode or self.model.mr is None:
            return
        mask = 1 if pressed else 0
        old, self._mrmask = self._mrmask, mask
        self._replay((self.model.mr,), mask, old)

    def _mkeys(self, mask: int) -> None:
        if self.profiles.current.x_mode:
            if self._ready:
                old, self._mmask = self._mmask, mask
                self._replay(self.model.mkeys, mask, old)
            return
        pressed, self._mmask = mask & ~self._mmask, mask
        for n in range(1, self.model.subprofiles + 1):
            if pressed & (1 << (n - 1)):
                self.spawn(self._select_subprofile(n))
                return

    def _brightness_changed(self, value: int) -> None:
        cur = self.profiles.current
        if value == cur.brightness or not 0 <= value <= 100:
            return
        cur.brightness = value
        self._save()
        self.publish({"brightness": value})

    def _release_all(self) -> None:
        self._gmask = self._mmask = self._mrmask = 0
        if self.host and self.host.input:
            for g in self.model.bindable:
                self.host.input.up(g.id)

    # -- actions ---------------------------------------------------------------------

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        handler = getattr(self, f"_do_{action}", None)
        log.debug("%s: action %s", self.channel.ident.uid, action)
        if handler is None:
            raise DriverError(f"unknown action {action!r}")
        try:
            return await handler(params)
        except (settings.SettingsError, ModelError) as e:
            raise DriverError(str(e)) from None
        except Quiet as e:
            self._went_quiet(e)  # saved and shown; applied when the keyboard is back
        except (HidppError, Unsupported) as e:
            raise DriverError(str(e)) from e

    @staticmethod
    def _param(params: dict[str, Any], name: str) -> Any:
        if name not in params:
            raise DriverError(f"missing parameter {name!r}")
        return params[name]

    async def _changed(self, apply: str | None) -> None:
        """Save, publish, and push ``apply`` (see ``_apply``) to the keyboard
        if it is listening."""
        self._save()
        self._resolve_chords()
        self.publish({**self._profile_state(), "status": self._status()})
        if self._ready and apply is not None:
            try:
                await self._apply(apply)
            except Quiet as e:
                # After a few idle minutes the keyboard can miss the first
                # request while its radio wakes up: one more try before
                # calling it asleep. _apply only sets values, so repeating it is safe.
                log.debug("%s: %s; trying once more", self.channel.ident.uid, e)
                await self._apply(apply)

    async def use_profile(self, profile: SharedProfile, known: set[str]) -> None:
        log.debug("%s: using profile %r", self.channel.ident.uid, profile.name)
        self._shared = (profile.id, profile.copy_of, set(known))
        self.profiles.use(profile.id, profile.copy_of, known)
        self.profile_name = profile.name
        self._release_all()
        try:
            await self._changed("all")
        except Quiet as e:
            self._went_quiet(e)

    async def _select_subprofile(self, n: int) -> None:
        cur = self.profiles.current
        if cur.subprofile == n:
            return
        self._release_all()
        cur.subprofile = n
        await self._changed("subprofile")

    async def _do_select_subprofile(self, params: dict[str, Any]) -> None:
        await self._select_subprofile(self.profiles.limits.subprofile(self._param(params, "value")))

    async def _do_set_x_mode(self, params: dict[str, Any]) -> None:
        on = settings.switch(self._param(params, "value"))
        self._release_all()
        self.profiles.current.x_mode = on
        await self._changed("subprofile")

    async def _do_set_x_light(self, params: dict[str, Any]) -> None:
        key, on = self._param(params, "key"), settings.switch(self._param(params, "value"))
        cur = self.profiles.current
        lit = set(cur.x_lights) - {key} | ({key} if on else set())
        cur.x_lights = self.profiles.limits.x_lights(sorted(lit))
        await self._changed("subprofile")

    async def _do_set_numlock_light(self, params: dict[str, Any]) -> None:
        self.profiles.current.numlock_light = settings.switch(self._param(params, "value"))
        await self._changed("lighting")

    async def _do_set_report_rate(self, params: dict[str, Any]) -> None:
        self.profiles.current.report_rate = self.profiles.limits.rate(self._param(params, "value"))
        await self._changed("rate")

    async def _do_set_brightness(self, params: dict[str, Any]) -> None:
        self.profiles.current.brightness = settings.brightness(self._param(params, "value"))
        await self._changed("brightness")

    async def _do_set_game_mode_keys(self, params: dict[str, Any]) -> None:
        self.profiles.current.game_mode_keys = self.profiles.limits.game_mode_keys(self._param(params, "keys"))
        await self._changed("game_mode")

    # G-keys (current subprofile)

    async def _do_set_binding(self, params: dict[str, Any]) -> None:
        g = self.model.gkey(self._param(params, "button"))
        value = settings.binding(self._param(params, "binding"))
        if self.host and self.host.input:
            self.host.input.up(g.id)
        self.profiles.current.sub.bindings[g.id] = value
        await self._changed("bindings")

    async def _do_reset_binding(self, params: dict[str, Any]) -> None:
        g = self.model.gkey(self._param(params, "button"))
        if self.host and self.host.input:
            self.host.input.up(g.id)
        self.profiles.current.sub.bindings[g.id] = copy.deepcopy(self.model.defaults.sub.bindings.get(g.id, "disabled"))
        await self._changed("bindings")

    # lighting (current subprofile)

    async def _do_set_lighting_mode(self, params: dict[str, Any]) -> None:
        self.profiles.current.sub.lighting.mode = settings.mode(self._param(params, "value"))
        await self._changed("lighting")

    async def _do_set_lighting_color(self, params: dict[str, Any]) -> None:
        self.profiles.current.sub.lighting.color = settings.color(self._param(params, "value"))
        await self._changed("lighting")

    async def _do_set_lighting_speed(self, params: dict[str, Any]) -> None:
        self.profiles.current.sub.lighting.speed = settings.speed(self._param(params, "value"))
        await self._changed("lighting")

    async def _do_set_key_colors(self, params: dict[str, Any]) -> None:
        keys = self._param(params, "keys")
        if not isinstance(keys, list) or not keys:
            raise DriverError("keys must list the keys to colour")
        leds = [self.profiles.limits.led(k) for k in keys]
        value = params.get("color")
        light = self.profiles.current.sub.lighting
        if value is None:
            for led in leds:
                light.keys.pop(led, None)
        else:
            c = settings.color(value)
            for led in leds:
                light.keys[led] = c
        await self._changed("lighting" if light.mode == "per_key" else None)

    async def _do_clear_key_colors(self, params: dict[str, Any]) -> None:
        light = self.profiles.current.sub.lighting
        light.keys.clear()
        await self._changed("lighting" if light.mode == "per_key" else None)

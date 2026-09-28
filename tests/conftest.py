from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from borochid.common.manifest import Manifest
from borochid.common.models import Bus, DeviceIdentity
from borochid.service.channels import Channel
from borochid.service.settings import MemoryStore

from borochid_logitech_keyboard.channel import NoLink
from borochid_logitech_keyboard.driver import KeyboardDriver

KEYS = [
    {"id": "esc", "label": "Esc", "x": 1, "y": 1, "led": 38, "usage": 41},
    {"id": "a", "label": "A", "x": 2, "y": 3, "led": 1, "usage": 4},
    {"id": "caps_lock", "label": "Caps Lock", "x": 1, "y": 3, "led": 54, "usage": 57},
    {"id": "left_super", "label": "Left Super", "x": 1, "y": 5, "led": 107, "usage": 227},
    {"id": "num_lock", "label": "Num Lock", "x": 20, "y": 2, "led": 80, "usage": 83},
    {"id": "logo", "label": "Logo", "x": 1, "y": 0, "led": 210},
    {"id": "m1", "label": "M1", "x": 3, "y": 0},
    *({"id": f"g{n}", "label": f"G{n}", "x": 0, "y": n, "led": 179 + n} for n in range(1, 6)),
]
LEDS = sorted(k["led"] for k in KEYS if "led" in k)

MANIFEST = {
    "id": "test.keyboard",
    "version": "1.0.0",
    "match": [{"bus": "usb", "vid": "0x046d", "pid": "0xc541"}],
    "channel": {"type": "logitech-hidpp-broker"},
    "driver": {"type": "logitech-hidpp-keyboard"},
    "keyboard": {"keys": KEYS},
    "hidpp_keyboard": {
        "gkeys": [{"id": f"g{n}", "label": f"G{n}", "bit": n} for n in range(1, 6)],
        "subprofiles": 3,
        "game_mode_locked": [227],
        "defaults": {
            "report_rate": 1000,
            "brightness": 100,
            "game_mode_keys": [],
            "bindings": {"g1": "disabled"},
            "lighting": {"mode": "per_key", "color": "#00b4ff", "speed": 5, "keys": {}},
        },
        "reply_timeout_s": 0.05,
    },
}

# Feature indexes as a G915 WIRELESS reports them.
INDEX = {
    0x0001: 1, 0x0003: 2, 0x0005: 3, 0x1D4B: 4, 0x0020: 5, 0x0007: 6, 0x1001: 7, 0x1814: 8, 0x1815: 9,
    0x8071: 10, 0x8081: 11, 0x1B04: 12, 0x1BC0: 13, 0x4100: 14, 0x4522: 15, 0x4540: 16, 0x8010: 17,
    0x8020: 18, 0x8030: 19, 0x8040: 20, 0x8100: 21, 0x8060: 22,
}  # fmt: skip
FEAT = {v: k for k, v in INDEX.items()}
INVALID_ARGUMENT, INVALID_FUNCTION = 2, 7

# RGB_EFFECTS GetInfo replies, as measured: zone 0 is the logo (location 2),
# zone 1 the keys (location 1); effect ids per index.
ZONES = {0: (2, [0x00, 0x01, 0x03, 0x0A]), 1: (1, [0x00, 0x01, 0x0A, 0x03, 0x04, 0x0B, 0x0C])}


class FakeKeyboard(Channel):
    """A G915 WIRELESS as the broker presents it, behaving as measured:

    * answers nothing while asleep, nor the next ``drop_next`` requests
      (a radio waking from idle);
    * G-keys, M-keys and MR report only once G-keys are diverted (host mode);
    * the brightness key changes brightness itself and notifies;
    * lighting shows only while the host holds SW control; a per-key frame
      shows on FrameEnd.
    """

    def __init__(self):
        super().__init__(DeviceIdentity(Bus.USB, "usb:1-3", vid=0x046D, pid=0xC541, attrs={"sys_path": "/sys/devices/x/usb1/1-3"}), {})
        self.on_link = lambda _up, _problem: None
        self.problem = None
        self.up = True
        self.asleep = False
        self.drop_next = 0
        self.missing: set[int] = set()  # features this connection lacks (REPORT_RATE over Bluetooth)
        self.unit = bytes.fromhex("9454DCB7")  # what DEVICE_FW_VERSION reports
        self.mode = 1
        self.diverted = False
        self.sw_control = (0, 0)
        self.effects: dict[int, tuple[int, bytes]] = {}  # zone -> (effect id, params)
        self.buffer: dict[int, tuple[int, int, int]] = {}
        self.leds: dict[int, tuple[int, int, int]] = {}  # what's lit after FrameEnd
        self.m_leds = 0
        self.mr_led = None
        self.brightness = 100
        self.rate_ms = 1
        self.disabled: set[int] = set()
        self.millivolts, self.battery_flags = 3835, 0
        self.calls: list[tuple[int, int, bytes]] = []

    async def open(self): ...

    async def close(self): ...

    def called(self, feature: int, function: int) -> list[bytes]:
        return [p for f, fn, p in self.calls if (f, fn) == (feature, function)]

    async def write(self, data: bytes) -> None:
        if not self.up:
            raise NoLink("broker down")
        assert len(data) == 20 and data[0] == 0x11
        index, fn, params = data[2], data[3] >> 4, data[4:]
        if self.asleep:
            return
        if self.drop_next:
            self.drop_next -= 1
            return
        feature = FEAT.get(index, 0) if index else 0
        self.calls.append((feature, fn, params.rstrip(b"\0")))
        reply = self.handle(index, feature, fn, params)
        if isinstance(reply, int) and reply < 0:
            out = bytes([0x11, 0x01, 0xFF, index, data[3], -reply])
        else:
            out = bytes([0x11, 0x01, index, data[3], *(reply or b"")])
        asyncio.get_running_loop().call_soon(self._deliver, out.ljust(20, b"\0")[:20])

    def handle(self, index: int, feature: int, fn: int, p: bytes):
        if index == 0:
            fid = (p[0] << 8) | p[1]
            return bytes([0 if fid in self.missing else INDEX.get(fid, 0), 0, 0]) if fn == 0 else bytes([4, 2, p[2]])
        if feature == 0x0001:  # FEATURE_SET, which the broker walks: count, then the ID at each index
            if fn == 0:
                return bytes([max(FEAT)])
            if fn == 1:
                fid = FEAT.get(p[0])
                return bytes([fid >> 8, fid & 0xFF, 0, 0]) if fid is not None else -INVALID_ARGUMENT
        if feature == 0x0003 and fn == 0:  # getDeviceInfo: entities, unit ID, transport, model
            return bytes([3, *self.unit, 0x00, 0x0B, 0x40, 0x9F, 0, 0, 0, 0])
        if feature == 0x1BC0:
            raise AssertionError("the driver must never touch REPORT_HID_USAGE")
        if feature == 0x8060:
            if fn == 0:
                return bytes([0x8B])
            if fn == 2:
                self.rate_ms = p[0]
                return b""
        if feature == 0x8071:
            if fn == 0:
                zone, effect = p[0], p[1]
                if zone == 0xFF:
                    return bytes([0xFF, 0, len(ZONES)])
                if zone not in ZONES:
                    return -INVALID_ARGUMENT
                location, effects = ZONES[zone]
                if effect == 0xFF:
                    return bytes([zone, 0, 0, location, len(effects)])
                return bytes([zone, effect, 0, effects[effect]])
            if fn == 1:
                assert p[12] == 0x01, "lighting must never be persisted"
                self.effects[p[0]] = (ZONES[p[0]][1][p[1]], bytes(p[2:12]))
                return b""
            if fn == 5:
                self.sw_control = (p[1], p[2]) if p[0] == 1 else self.sw_control
                return bytes([0, *self.sw_control])
            if fn == 3:
                raise AssertionError("the driver must never write boot effects (NvConfig)")
        if feature == 0x8081:
            if fn == 6:
                for led in p[3:16]:
                    if led:
                        self.buffer[led] = (p[0], p[1], p[2])
                return b""
            if fn == 7:
                self.leds.update(self.buffer)
                self.buffer = {}
                return b""
        if feature == 0x8010 and fn == 2:
            self.diverted = bool(p[0])
            return b""
        if feature == 0x8020 and fn == 1:
            self.m_leds = p[0]
            return b""
        if feature == 0x8030 and fn == 0:
            self.mr_led = p[0]
            return b""
        if feature == 0x8040:
            if fn == 1:
                return bytes([self.brightness >> 8, self.brightness & 0xFF])
            if fn == 2:
                self.brightness = (p[0] << 8) | p[1]
                return b""
        if feature == 0x4522:
            if fn == 1:
                self.disabled |= {u for u in p if u}
                return b""
            if fn == 3:
                self.disabled = set()
                return b""
        if feature == 0x8100:
            if fn == 1:
                self.mode = p[0]
                return b""
            if fn == 2:
                return bytes([self.mode])
            if fn in (3, 5, 6, 7, 8):
                raise AssertionError("the driver must never touch onboard profile memory")
        if feature == 0x1001 and fn == 0:
            return bytes([self.millivolts >> 8, self.millivolts & 0xFF, self.battery_flags])
        return -INVALID_FUNCTION

    # -- things the user does --------------------------------------------------

    def _notify(self, feature: int, *params: int) -> None:
        self._deliver(bytes([0x11, 0x01, INDEX[feature], 0x00, *params]).ljust(20, b"\0"))

    def press_g(self, mask: int) -> None:
        """G-keys held now, as a mask (G1 = 1)."""
        if self.mode == 2 and self.diverted:
            self._notify(0x8010, mask & 0xFF, mask >> 8, 0, 0)

    def press_m(self, n: int) -> None:
        if self.mode == 2 and self.diverted:
            self._notify(0x8020, 1 << (n - 1))
            self._notify(0x8020, 0)

    def press_mr(self) -> None:
        if self.mode == 2 and self.diverted:
            self._notify(0x8030, 1)
            self._notify(0x8030, 0)

    def brightness_key(self) -> None:
        self.brightness = 50 if self.brightness == 100 else 100
        self._notify(0x8040, self.brightness >> 8, self.brightness & 0xFF)

    def link(self, up: bool) -> None:
        """The receiver's HID++ 1.0 connection report."""
        if up:
            self.asleep = False
        self._deliver(bytes([0x10, 0x01, 0x41, 0x0C, 0x00 if up else 0x40, 0x7C, 0x40]))

    def power_cycle(self) -> None:
        self.mode, self.diverted, self.sw_control, self.effects, self.leds = 1, False, (0, 0), {}, {}
        self.link(True)

    def broker(self, up: bool) -> None:
        self.up = up
        self.problem = None if up else "broker down"
        self.on_link(up, self.problem)


class FakeInput:
    def __init__(self):
        self.events: list[tuple] = []
        self.held: set = set()
        self.is_open = False

    async def open(self):
        self.is_open = True

    def close(self):
        for token in list(self.held):
            self.up(token)
        self.is_open = False

    def down(self, token, chord):
        if token not in self.held:
            self.held.add(token)
            self.events.append(("down", token, chord.to_json()))

    def up(self, token):
        if token in self.held:
            self.held.discard(token)
            self.events.append(("up", token))


class FakeHost:
    def __init__(self):
        self.input = FakeInput()


def make_driver(settings=None, store=None, **spec):
    kb = FakeKeyboard()
    events: list[dict] = []
    m = copy.deepcopy(MANIFEST)
    m["hidpp_keyboard"].update(spec)
    store = store if store is not None else MemoryStore(settings)
    driver = KeyboardDriver(Manifest.from_json(m), Path("."), kb, events.append, store, FakeHost())
    kb.on_data = driver.on_data
    return driver, kb, events, store


async def settle(driver, rounds: int = 3) -> None:
    """Let the driver finish what it is doing (all replies are immediate)."""
    for _ in range(rounds):
        for _ in range(50):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)


@pytest.fixture
def run():
    def runner(coro):
        return asyncio.run(asyncio.wait_for(coro, 5))

    return runner

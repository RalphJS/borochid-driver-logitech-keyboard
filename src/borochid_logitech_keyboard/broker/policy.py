"""What the HID++ broker lets through, in both directions. Pure: no I/O,
standard library only.

Why this exists: the G915 has feature ``0x1BC0`` (REPORT_HID_USAGE), which
makes it report every key press over HID++, on the same node Borochid needs
for lighting and G-keys. Whoever can write arbitrary HID++ to that node can
read everything typed. So the node belongs to the broker's own system user,
and the Borochid service (running as the logged-in user) only gets what
this table allows. Nothing outside it is reachable, however the requests
are crafted:

* **The broker discovers feature indexes itself; the client can't
  influence them.** HID++ addresses a feature by a per-device index. The
  broker walks the keyboard's FEATURE_SET (``Discovery``): each reply names
  the feature ID at an index, so it describes itself, and the round is
  strictly serialised under a software ID only the broker uses (the client
  may not). Only a completed round replaces the table. Learning from the
  client's own ROOT getFeature replies would not be safe: those replies
  don't echo the feature ID, so a late reply to one query could be taken
  for the answer to the next, and the checks for one feature would then
  guard another. The client's ROOT getFeature queries are answered by the
  broker from its table, never forwarded, and a feature outside the
  allowlist reads as "not supported": its index is never revealed, and a
  request to any index not in the table is refused.
* **Functions and arguments are checked per feature.** Setters that would
  write the keyboard's non-volatile memory (onboard profile memory, NvConfig
  boot effects, the "persist" flag of an effect) are refused: Borochid never
  writes flash.
* **The client never picks the device index**: the broker writes to the
  keyboard only: index 1 behind a receiver (never the receiver itself,
  0xFF, or other slots), or 0xFF for a keyboard on a cable, which is the
  keyboard itself.
* **Only replies to the client's own requests and a few notifications go
  back**: G/M/MR keys, brightness, battery, link state. Anything else the
  node carries is dropped.

A refused request is answered like the device would answer an unsupported
one (HID++ 2.0 error UNSUPPORTED), so the client's session code needs no
special case.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

LONG, SHORT = 0x11, 0x10
LONG_SIZE = 20
KEYBOARD = 0x01  # the paired device's slot on a LIGHTSPEED receiver
WIRED = 0xFF  # a keyboard on a cable answers as the device itself
ERROR_20, ERROR_10 = 0xFF, 0x8F
UNSUPPORTED = 0x09
CONNECT, DISCONNECT = 0x41, 0x40  # HID++ 1.0 receiver notifications (sub id)

ROOT = 0x0000
FEATURE_SET = 0x0001

# Software IDs only the broker uses; client requests with them are refused,
# and replies carrying them are never forwarded. Discovery rotates through
# DISCOVERY_SW round by round, so a stale reply from an aborted round can't
# match the next one; RESTORE_SW is for the hand-back on disconnect.
DISCOVERY_SW = (0x0C, 0x0D, 0x0E)
RESTORE_SW = 0x0F
RESERVED_SW = frozenset((*DISCOVERY_SW, RESTORE_SW))
Params = bytes
Check = Callable[[Params], bool]
ANY: Check = lambda _p: True  # noqa: E731


def _first_in(*allowed: int) -> Check:
    return lambda p: p[0] in allowed


# Feature ID -> {function: argument check}. Anything missing is refused.
# Measured on a G915 WIRELESS unless noted; see the driver README.
ALLOWED: dict[int, dict[int, Check]] = {
    # ROOT: fn0 getFeature is answered by the broker (see Policy); fn1 ping.
    ROOT: {1: ANY},
    0x0003: {0: ANY, 1: ANY},  # DEVICE_FW_VERSION: getters
    0x0005: {0: ANY, 1: ANY, 2: ANY},  # DEVICE_NAME: getters
    0x1001: {0: ANY},  # BATTERY_VOLTAGE: get voltage
    0x1D4B: {},  # WIRELESS_DEVICE_STATUS: notifications only
    # DISABLE_KEYS_BY_USAGE: which keys game mode disables (0 caps, 1 disable,
    # 2 enable, 3 enable all). Held in RAM; only active while the keyboard's
    # own game-mode switch is on.
    0x4522: {0: ANY, 1: ANY, 2: ANY, 3: ANY},
    0x4540: {0: ANY},  # KEYBOARD_INTERNATIONAL_LAYOUTS: get layout
    0x8010: {0: ANY, 2: _first_in(0, 1)},  # GKEY: count; divert off/on
    0x8020: {0: ANY, 1: ANY},  # MKEYS: count; LED mask
    0x8030: {0: _first_in(0, 1)},  # MR: LED off/on
    0x8040: {0: ANY, 1: ANY, 2: ANY},  # BRIGHTNESS_CONTROL: info, get, set
    0x8060: {0: ANY, 1: ANY, 2: ANY},  # REPORT_RATE: list, get, set
    # RGB_EFFECTS. fn1 setEffect ends with a persistence byte (params[12]):
    # 0x01 applies the effect in RAM only, 0 would make it the power-on
    # effect. fn5 SW control: get, take control (mode 3, flags 4) or hand it
    # back (mode 0). Never fn3 (NvConfig: persistent boot/shutdown effects),
    # fn7 (idle timers), fn8/fn9 (power modes).
    0x8071: {
        0: ANY,
        1: lambda p: p[12] == 0x01,
        5: lambda p: p[0] == 0 or tuple(p[:3]) in ((1, 3, 4), (1, 0, 0)),
    },
    # PER_KEY_LIGHTING_V2: 0 info, 1 set up to 4 keys, 5 set a range,
    # 6 one colour for up to 13 keys, 7 FrameEnd (commit). FrameEnd's
    # argument bytes must be 0 (the only form the keyboard is known to take);
    # other functions are unknown and refused.
    0x8081: {0: ANY, 1: ANY, 5: ANY, 6: ANY, 7: lambda p: not any(p[:5])},
    # ONBOARD_PROFILES: 0 description, 2 get mode, 1 set mode to onboard (1)
    # or host (2). Everything else here reads or writes profile memory.
    0x8100: {0: ANY, 2: ANY, 1: _first_in(1, 2)},
}

# Features whose notifications (software id 0) reach the client.
NOTIFY = frozenset({0x1001, 0x1D4B, 0x8010, 0x8020, 0x8030, 0x8040})

# Sent when the client goes away (the service crashed or stopped), in this
# order, for features that were learned: stop diverting keys, lights off on
# M/MR, give lighting back, clear the game-mode list, onboard mode last.
RESTORE: tuple[tuple[int, int, tuple[int, ...]], ...] = (
    (0x8010, 2, (0,)),
    (0x8030, 0, (0,)),
    (0x8020, 1, (0,)),
    (0x8071, 5, (1, 0, 0)),
    (0x4522, 3, ()),
    (0x8100, 1, (1,)),
)

PENDING_MAX = 16

# What check_request decides.
FORWARD = "forward"  # send it to the keyboard (after rewrite())
ANSWER = "answer"  # send the returned report to the client instead
DEFER = "defer"  # a ROOT query before discovery finished: answer it once it has


def error_reply(feature_index: int, function_sw: int) -> bytes:
    return bytes([LONG, 0xFF, ERROR_20, feature_index, function_sw, UNSUPPORTED]).ljust(LONG_SIZE, b"\0")


def build_request(feature_index: int, function: int, sw_id: int, params: tuple[int, ...] | bytes = (),
                  device: int = KEYBOARD) -> bytes:
    return bytes([LONG, device, feature_index, (function << 4) | sw_id, *params]).ljust(LONG_SIZE, b"\0")


def is_reply(report: bytes, index: int, function: int, sw: int, device: int = KEYBOARD) -> bool:
    """A reply, or an error reply, to the request (index, function, sw)."""
    if len(report) < 6 or report[0] not in (SHORT, LONG) or report[1] != device:
        return False
    fn_sw = (function << 4) | sw
    if report[2] in (ERROR_20, ERROR_10):
        return report[3] == index and report[4] == fn_sw
    return report[0] == LONG and report[2] == index and report[3] == fn_sw


class Discovery:
    """One round of feature discovery, as a state machine; the server does
    the I/O. Strictly one request at a time::

        ROOT getFeature(FEATURE_SET)  -> its index
        FEATURE_SET fn0               -> feature count
        FEATURE_SET fn1(i), i = 1..n  -> feature ID, type, version at index i

    ``request()`` is what to send next (None when finished), ``matches()``
    tells its reply apart from other traffic, ``feed()`` takes that reply
    and returns False if the round must be abandoned (an error, a malformed
    reply); a missing reply abandons it too. ``table`` is complete only once
    ``done``, and holds allowed features only: forbidden ones are never in
    it.
    """

    def __init__(self, sw: int, device: int = KEYBOARD):
        if sw not in DISCOVERY_SW:
            raise ValueError("discovery must use a reserved software id")
        self.sw = sw
        self.device = device
        self.table: dict[int, tuple[int, int, int]] = {}  # index -> (feature id, type, version)
        self.done = False
        self._feature_set: int | None = None
        self._count: int | None = None
        self._next = 1  # FEATURE_SET index being asked for

    def _step(self) -> tuple[int, int, tuple[int, ...]] | None:
        if self.done:
            return None
        if self._feature_set is None:
            return 0, 0, (FEATURE_SET >> 8, FEATURE_SET & 0xFF)
        if self._count is None:
            return self._feature_set, 0, ()
        return self._feature_set, 1, (self._next,)

    def request(self) -> bytes | None:
        step = self._step()
        return None if step is None else build_request(step[0], step[1], self.sw, step[2], self.device)

    def matches(self, report: bytes) -> bool:
        step = self._step()
        return step is not None and is_reply(report, step[0], step[1], self.sw, self.device)

    def feed(self, report: bytes) -> bool:
        if not self.matches(report) or report[2] in (ERROR_20, ERROR_10):
            return False
        params = report[4:]
        if self._feature_set is None:
            if params[0] in (0, ERROR_20):
                return False  # no FEATURE_SET: nothing can be discovered
            self._feature_set = params[0]
        elif self._count is None:
            self._count = params[0]
        else:
            feature = (params[0] << 8) | params[1]
            if feature in ALLOWED and feature != ROOT:
                if any(f == feature for f, _, _ in self.table.values()):
                    return False  # listed twice: not a table to trust
                self.table[self._next] = (feature, params[2], params[3])
            self._next += 1
        if self._count is not None and self._next > self._count:
            self.done = True
        return True


class Policy:
    """State for one open device: the feature table (from Discovery only)
    and the client's requests still waiting for a reply. ``device`` is the
    keyboard's device index: KEYBOARD behind a receiver, WIRED on a cable."""

    def __init__(self, device: int = KEYBOARD) -> None:
        self.device = device
        self.features: dict[int, tuple[int, int, int]] = {}  # index -> (feature id, type, version)
        self.ready = False
        self._pending: deque[tuple[int, int, int]] = deque(maxlen=PENDING_MAX)

    @property
    def index_to_id(self) -> dict[int, int]:
        return {0: ROOT, **{i: f[0] for i, f in self.features.items()}}

    def index_of(self, feature_id: int) -> int | None:
        return next((i for i, f in self.features.items() if f[0] == feature_id), None)

    def install(self, table: dict[int, tuple[int, int, int]]) -> None:
        """Replace the feature table with a completed discovery's, at once."""
        clean = {i: f for i, f in table.items() if 0 < i < 0xFF and f[0] in ALLOWED and f[0] != ROOT}
        self.features = clean
        self.ready = True

    # -- client -> device ---------------------------------------------------------

    def check_request(self, report: bytes) -> tuple[str, bytes | None]:
        """What to do with a client request: (FORWARD, None) and remember
        it as pending; (ANSWER, report) to send the client instead (an error
        reply, or a ROOT getFeature answered from the table); or (DEFER,
        None) for a ROOT getFeature that must wait for discovery."""
        if len(report) != LONG_SIZE or report[0] != LONG:
            return ANSWER, error_reply(report[2] if len(report) > 2 else 0, report[3] if len(report) > 3 else 0)
        index, fn_sw, params = report[2], report[3], report[4:]
        function, sw = fn_sw >> 4, fn_sw & 0x0F
        refuse = ANSWER, error_reply(index, fn_sw)
        # 0 marks notifications; the reserved ones are the broker's own.
        if sw == 0 or sw in RESERVED_SW:
            return refuse
        if index == 0 and function == 0:
            return (ANSWER, self.answer_root(report)) if self.ready else (DEFER, None)
        feature = self.index_to_id.get(index)
        if feature is None or feature not in ALLOWED:
            return refuse
        check = ALLOWED[feature].get(function)
        if check is None or not check(params):
            return refuse
        self._pending.append((index, function, sw))
        return FORWARD, None

    def answer_root(self, report: bytes) -> bytes:
        """The reply to a client's ROOT getFeature, from the table: index,
        type and version, or all zero ("not supported") for anything not in
        it, forbidden features included."""
        wanted = (report[4] << 8) | report[5]
        found = next(((i, t, v) for i, (f, t, v) in self.features.items() if f == wanted), (0, 0, 0))
        return bytes([LONG, self.device, 0, report[3], *found]).ljust(LONG_SIZE, b"\0")

    def rewrite(self, report: bytes) -> bytes:
        """Address the keyboard, whatever device index the client wrote."""
        return report[:1] + bytes([self.device]) + report[2:]

    # -- device -> client ---------------------------------------------------------

    def check_incoming(self, report: bytes) -> bool:
        """Whether a report read from the node goes to the client."""
        if len(report) < 5 or report[0] not in (SHORT, LONG) or report[1] != self.device:
            return False  # the receiver's own replies (0xFF) and anything else
        sub = report[2]
        if report[0] == SHORT and sub in (CONNECT, DISCONNECT) and self.device == KEYBOARD:
            return True  # link up/down, from the receiver on the keyboard's behalf
        if sub in (ERROR_20, ERROR_10):
            return self._take(report[3], report[4] >> 4, report[4] & 0x0F)
        index, function, sw = sub, report[3] >> 4, report[3] & 0x0F
        if sw == 0:
            return self.index_to_id.get(index) in NOTIFY
        return self._take(index, function, sw)

    def _take(self, index: int, function: int, sw: int) -> bool:
        if sw in RESERVED_SW:
            return False
        try:
            self._pending.remove((index, function, sw))
        except ValueError:
            return False
        return True

    # -- cleanup ----------------------------------------------------------------------

    def restore_requests(self) -> list[tuple[int, int, tuple[int, ...]]]:
        return [r for r in RESTORE if self.index_of(r[0]) is not None]

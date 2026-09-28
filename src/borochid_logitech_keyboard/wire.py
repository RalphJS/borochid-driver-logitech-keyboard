"""What the Borochid service and the HID++ broker say to each other over the
broker's Unix socket. Pure: no I/O, standard library only, because the
broker imports it too.

1. The client opens one device with a JSON line naming its USB device (the
   receiver's, or the keyboard's on its cable: the sysfs name, e.g. ``1-3``)
   or, over Bluetooth, the keyboard's address::

       {"v": 1, "open": "1-3"}
       {"v": 1, "open": "bt:ef:24:28:5f:95:8a"}

   The broker answers with one JSON line, ``{"ok": true}`` or
   ``{"ok": false, "error": "...", "retry": true}``; ``retry`` means the
   device isn't ready yet (the node is still being set up, the receiver
   was unplugged), as opposed to never allowed. Behind a receiver, ``ok``
   carries ``"unit": "9454DCB7"``, the paired keyboard's unit ID from the
   receiver's pairing record, known even while the keyboard is away.

2. After ``ok``, both sides send frames: one length byte (1-64), then that
   many bytes of a HID++ report. The client sends HID++ 2.0 requests for
   the keyboard; the broker picks the device index, so the client's is
   ignored. The broker sends back replies to those requests and the
   notifications its policy lets through. A refused request is answered
   with an HID++ 2.0 error report, like one from the device.
"""

from __future__ import annotations

import json
import re

VERSION = 1
SOCKET_PATH = "/run/borochid-hidpp/broker.sock"
MAX_FRAME = 64
MAX_LINE = 256
_USB_NAME_RE = re.compile(r"^[0-9]+-[0-9]+(\.[0-9]+)*$")
_BT_NAME_RE = re.compile(r"^bt:[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(_USB_NAME_RE.match(name) or _BT_NAME_RE.match(name))


class WireError(ValueError):
    pass


def hello(name: str) -> bytes:
    if not _valid_name(name):
        raise WireError(f"not a USB device name or Bluetooth address: {name!r}")
    return json.dumps({"v": VERSION, "open": name}).encode() + b"\n"


def parse_hello(line: bytes) -> str:
    """The device a client asks for (USB name or ``bt:`` address); raises WireError."""
    if len(line) > MAX_LINE:
        raise WireError("hello too long")
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise WireError("hello is not JSON") from None
    if not isinstance(msg, dict) or msg.get("v") != VERSION:
        raise WireError(f"unsupported protocol version {msg.get('v') if isinstance(msg, dict) else None!r}")
    name = msg.get("open")
    if not _valid_name(name):
        raise WireError("hello must name a USB device or a Bluetooth address")
    return name


_UNIT_RE = re.compile(r"^[0-9A-F]{8}$")


def answer(ok: bool, error: str | None = None, retry: bool = False, unit: str | None = None) -> bytes:
    msg: dict = {"ok": True} if ok else {"ok": False, "error": error or "refused", "retry": retry}
    if ok and unit is not None:
        msg["unit"] = unit
    return json.dumps(msg).encode() + b"\n"


def answer_unit(line: bytes) -> str | None:
    """The keyboard's unit ID in an ``ok`` answer, if well-formed."""
    try:
        unit = json.loads(line).get("unit")
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return unit if isinstance(unit, str) and _UNIT_RE.match(unit) else None


def parse_answer(line: bytes) -> tuple[bool, str | None, bool]:
    """(ok, error, retry); raises WireError on garbage."""
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise WireError("answer is not JSON") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("ok"), bool):
        raise WireError("malformed answer")
    return msg["ok"], (str(msg["error"]) if "error" in msg else None), bool(msg.get("retry", False))


def frame(report: bytes) -> bytes:
    if not 1 <= len(report) <= MAX_FRAME:
        raise WireError(f"report of {len(report)} bytes")
    return bytes([len(report)]) + report


class FrameReader:
    """Reassembles frames from a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out = []
        while self._buf:
            n = self._buf[0]
            if not 1 <= n <= MAX_FRAME:
                raise WireError(f"bad frame length {n}")
            if len(self._buf) < n + 1:
                break
            out.append(bytes(self._buf[1 : n + 1]))
            del self._buf[: n + 1]
        return out

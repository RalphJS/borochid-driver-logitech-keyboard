"""The HID++ broker daemon: the only process that opens the keyboard's HID++
node, handing the Borochid service a filtered pipe to it (see policy.py for
why and what passes).

It runs as its own unprivileged system user (``borochid-hidpp``), which udev
makes the node's group; the logged-in user has no access to the node. The
socket is world-connectable and access is decided here: a client must be
root or the user of the active session on seat0 (SO_PEERCRED, checked
against logind's seat file).

Per connection: the client names the receiver's USB device (wire.py), the
broker checks it is a supported receiver, opens its HID++ interface, turns
on the receiver's notifications (without them it forwards nothing from the
keyboard), and pumps reports through the policy until either side goes
away. It finds the keyboard's feature indexes itself (policy.Discovery),
when it opens the node and again whenever the keyboard shows life after a
failed round or reconnects. Then, when the client leaves, it puts the keyboard back as it would be without Borochid
(onboard mode, keys not diverted, lighting released) and restores the
receiver's notification flags. So a crashed service can't leave the
keyboard half taken over.

Standard library only; nothing from Borochid is imported but wire.py.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

from borochid_logitech_keyboard import wire
from borochid_logitech_keyboard.broker import policy
from borochid_logitech_keyboard.broker.policy import ANSWER, DEFER, Discovery, Policy

log = logging.getLogger("borochid-hidpp-broker")

# USB devices whose HID++ interface the broker serves: (vendor, product) as
# sysfs writes them, and the keyboard's device index on it. c541: the G915
# WIRELESS's LIGHTSPEED receiver, which the kernel leaves to hid-generic
# (no per-device node); the keyboard is its slot 1. c33e: the G915 itself
# on its USB cable, which answers as the device (0xFF). The same IDs must
# be in the udev rule that gives the broker the node.
SUPPORTED = {("046d", "c541"): policy.KEYBOARD, ("046d", "c33e"): policy.WIRED}
# Over Bluetooth (BlueZ's HID-over-GATT, a uhid device): b354, the G915
# itself. Its one HID device carries the keystrokes as well as HID++ (report
# 0x11); the policy passes on HID++ reports only.
SUPPORTED_BT = {("046d", "b354"): policy.WIRED}
HIDPP_INTERFACE = 2  # the vendor-page interface; 0 and 1 carry keystrokes and are never opened

SYSFS_USB = Path("/sys/bus/usb/devices")
SYSFS_HID = Path("/sys/bus/hid/devices")
DEV = Path("/dev")
SEAT_FILE = Path("/run/systemd/seats/seat0")
_HIDRAW_RE = re.compile(r"^hidraw[0-9]+$")

RECEIVER = 0xFF
NOTIFY_FLAGS = (0x00, 0x09, 0x00)  # wireless + software present
HELLO_TIMEOUT = 5.0
REPLY_TIMEOUT = 0.5
DISCOVERY_SPACING = 2.0  # seconds between discovery rounds
RATE = 200.0  # requests per second from a client, sustained
BURST = 64


class Refused(Exception):
    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


# -- who may connect -------------------------------------------------------------------


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def active_uid(seat_file: Path) -> int | None:
    """The uid of the active session on the seat, from logind's state file."""
    try:
        text = seat_file.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "ACTIVE_UID" and value.isdigit():
            return int(value)
    return None


# -- which node --------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text().strip().lower()
    except OSError:
        return ""


def _uevent(path: Path) -> dict[str, str]:
    try:
        lines = (path / "uevent").read_text().splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def find_bt_node(name: str, hid_sysfs: Path = SYSFS_HID) -> tuple[str, int]:
    """The hidraw node of a supported keyboard connected over Bluetooth, by
    its address (``bt:ef:24:...``), and its device index (itself, 0xFF)."""
    address = name.removeprefix("bt:")
    for hid in sorted(hid_sysfs.glob("0005:*")):  # 0005: the Bluetooth bus
        props = _uevent(hid)
        parts = props.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        ids = (parts[1][-4:].lower(), parts[2][-4:].lower())
        if (device := SUPPORTED_BT.get(ids)) is None:
            continue
        if props.get("HID_UNIQ", "").lower() != address:
            continue
        for node in sorted(hid.glob("hidraw/hidraw*")):
            if _HIDRAW_RE.match(node.name):
                return node.name, device
        raise Refused(f"{name} has no HID++ node yet", retry=True)
    # Not connected right now (asleep, out of range, switched to another channel).
    raise Refused(f"no supported keyboard connected at {name}", retry=True)


def find_node(usb_name: str, sysfs: Path = SYSFS_USB, hid_sysfs: Path = SYSFS_HID) -> tuple[str, int]:
    """The hidraw name (``hidrawN``) of a supported device's HID++
    interface, and the keyboard's device index on it. Refused(retry=True)
    while it isn't there (yet)."""
    if usb_name.startswith("bt:"):
        return find_bt_node(usb_name, hid_sysfs)
    usb = sysfs / usb_name
    if not usb.exists():
        raise Refused(f"no USB device {usb_name}", retry=True)
    device = SUPPORTED.get((_read(usb / "idVendor"), _read(usb / "idProduct")))
    if device is None:
        raise Refused(f"USB device {usb_name} is not a supported keyboard or receiver")
    usb = usb.resolve()
    for iface in sorted(usb.glob(f"{usb_name}:*")):
        if _read(iface / "bInterfaceNumber") != f"{HIDPP_INTERFACE:02x}":
            continue
        for node in sorted(iface.glob("*/hidraw/hidraw*")):
            if _HIDRAW_RE.match(node.name):
                return node.name, device
    raise Refused(f"{usb_name} has no HID++ node yet", retry=True)


def open_hidraw(name: str, dev: Path = DEV) -> int:
    if not _HIDRAW_RE.match(name):
        raise Refused(f"bad node name {name!r}")
    try:
        return os.open(dev / name, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise Refused(f"{name} disappeared", retry=True) from None
    except PermissionError:
        raise Refused(f"no access to {name}; is the udev rule installed?", retry=True) from None


# -- the node ------------------------------------------------------------------------------


class Node:
    """A hidraw file descriptor: reports go to ``listener``, except the one
    a ``transact`` is waiting for."""

    def __init__(self, fd: int):
        self.fd = fd
        self.listener: Callable[[bytes], None] = lambda _r: None
        self.lost = asyncio.Event()
        self._waiter: tuple[Callable[[bytes], bool], asyncio.Future] | None = None
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            report = os.read(self.fd, 64)
        except BlockingIOError:
            return
        except OSError as e:
            log.info("node closed: %s", e)
            self._lose()
            return
        if not report:
            self._lose()
            return
        if self._waiter and not self._waiter[1].done() and self._waiter[0](report):
            self._waiter[1].set_result(report)
            return
        self.listener(report)

    def _lose(self) -> None:
        asyncio.get_running_loop().remove_reader(self.fd)
        self.lost.set()

    def write(self, report: bytes) -> None:
        try:
            os.write(self.fd, report)
        except OSError as e:
            log.info("write to node failed: %s", e)
            self._lose()

    async def transact(self, report: bytes, match: Callable[[bytes], bool], timeout: float = REPLY_TIMEOUT) -> bytes | None:
        if self.lost.is_set():
            return None
        fut = asyncio.get_running_loop().create_future()
        self._waiter = (match, fut)
        try:
            self.write(report)
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiter = None

    def close(self) -> None:
        if not self.lost.is_set():
            asyncio.get_running_loop().remove_reader(self.fd)
        with contextlib.suppress(OSError):
            os.close(self.fd)


def _register(sub: int, reg: int, *params: int) -> bytes:
    return bytes([policy.SHORT, RECEIVER, sub, reg, *params]).ljust(7, b"\0")


def _is_register_reply(sub: int, reg: int) -> Callable[[bytes], bool]:
    return lambda r: len(r) >= 7 and r[1] == RECEIVER and (
        (r[2] == sub and r[3] == reg) or (r[2] == policy.ERROR_10 and r[3] == sub and r[4] == reg)
    )


PAIRING_INFO = 0xB5  # receiver register: pairing records, read-only here
PAIRING_EXTENDED = 0x30  # + slot - 1: the paired device's serial (unit ID) and more


async def read_paired_unit(node: Node) -> str | None:
    """The keyboard's unit ID as its receiver recorded it when pairing
    (register 0xB5, page 0x30 for slot 1): known even while the keyboard is
    off or on its cable, which is how the service knows them for one."""
    page = PAIRING_EXTENDED + policy.KEYBOARD - 1
    r = await node.transact(
        _register(0x83, PAIRING_INFO, page),
        lambda r: len(r) >= 9 and r[1] == RECEIVER and (
            (r[2] == 0x83 and r[3] == PAIRING_INFO and r[4] == page)
            or (r[2] == policy.ERROR_10 and r[3] == 0x83 and r[4] == PAIRING_INFO)
        ),
    )
    if r is None or r[2] != 0x83 or not any(r[5:9]):
        return None
    return bytes(r[5:9]).hex().upper()


async def read_flags(node: Node) -> tuple[int, int, int] | None:
    r = await node.transact(_register(0x81, 0x00), _is_register_reply(0x81, 0x00))
    if r is None or r[2] != 0x81:
        return None
    return r[4], r[5], r[6]


async def write_flags(node: Node, flags: tuple[int, int, int]) -> bool:
    r = await node.transact(_register(0x80, 0x00, *flags), _is_register_reply(0x80, 0x00))
    return r is not None and r[2] == 0x80


# -- one client's session ----------------------------------------------------------------


class Session:
    """Routes reports between one client and the node through the policy,
    and runs feature discovery rounds."""

    def __init__(self, node: Node, writer: asyncio.StreamWriter, name: str, device: int = policy.KEYBOARD):
        self.node = node
        self.writer = writer
        self.name = name
        self.rules = Policy(device)
        self._task: asyncio.Task | None = None
        self._round = 0
        self._last_round = -DISCOVERY_SPACING
        self._stale = True  # the table needs (re)discovering
        # The client's ROOT query waiting for discovery. Only the latest one,
        # and only while the client has sent nothing since: the client waits
        # for one reply at a time, so anything newer means it gave up on this
        # one, and a late answer could be taken for the reply to another.
        self._deferred: bytes | None = None

    def send(self, report: bytes) -> None:
        self.writer.write(wire.frame(report))

    def from_client(self, report: bytes) -> None:
        self._deferred = None
        action, out = self.rules.check_request(report)
        if action == ANSWER:
            if out[2] == policy.ERROR_20:
                log.debug("refused %s", report.hex(" "))
            self.send(out)
        elif action == DEFER:
            self._deferred = report
            self.discover()
        else:
            self.node.write(self.rules.rewrite(report))

    def from_node(self, report: bytes) -> None:
        if len(report) >= 3 and report[1] == self.rules.device:
            if report[0] == policy.SHORT and report[2] == policy.CONNECT:
                self._stale = True  # (re)connected: maybe another firmware
            if self._stale:
                self.discover()
        if self.rules.check_incoming(report):
            self.send(report)

    def discover(self) -> None:
        """Start a discovery round unless one is running or the last one
        started less than DISCOVERY_SPACING ago."""
        now = time.monotonic()
        if (self._task and not self._task.done()) or now - self._last_round < DISCOVERY_SPACING:
            return
        self._last_round = now
        sw = policy.DISCOVERY_SW[self._round % len(policy.DISCOVERY_SW)]
        self._round += 1
        self._task = asyncio.ensure_future(self._discover(sw))

    async def _discover(self, sw: int) -> None:
        d = Discovery(sw, self.rules.device)
        while (request := d.request()) is not None:
            reply = await self.node.transact(request, d.matches)
            if reply is None or not d.feed(reply):
                log.info("%s: feature discovery %s; retrying when the keyboard is back", self.name, "timed out" if reply is None else "failed")
                return
        self.rules.install(d.table)
        self._stale = False
        log.info("%s: %d allowed features found", self.name, len(d.table))
        if self._deferred is not None:
            self.send(self.rules.answer_root(self._deferred))
            self._deferred = None
            with contextlib.suppress(OSError):
                await self.writer.drain()

    async def stop_discovery(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


# -- the broker ----------------------------------------------------------------------------


class Broker:
    def __init__(
        self,
        sysfs: Path = SYSFS_USB,
        seat_file: Path = SEAT_FILE,
        hid_sysfs: Path = SYSFS_HID,
        opener: Callable[[str], int] = open_hidraw,
        rate: float = RATE,
        burst: int = BURST,
    ):
        self.sysfs = sysfs
        self.hid_sysfs = hid_sysfs
        self.seat_file = seat_file
        self.opener = opener
        self.rate = rate
        self.burst = burst
        self.busy: set[str] = set()

    async def serve(self, sock: socket.socket) -> None:
        server = await asyncio.start_unix_server(self.handle, sock=sock, limit=wire.MAX_LINE)
        async with server:
            await server.serve_forever()

    def allowed(self, uid: int) -> bool:
        return uid == 0 or uid == active_uid(self.seat_file)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._session(reader, writer)
        except Refused as e:
            log.info("refused: %s", e)
            with contextlib.suppress(OSError):
                writer.write(wire.answer(False, str(e), e.retry))
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError, ValueError) as e:
            log.info("client dropped: %s", e)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = peer_uid(writer.get_extra_info("socket"))
        if not self.allowed(uid):
            raise Refused(f"uid {uid} is not the active session's user")
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
        except (TimeoutError, ValueError):
            raise Refused("no hello") from None
        try:
            usb_name = wire.parse_hello(line)
        except wire.WireError as e:
            raise Refused(str(e)) from None
        name, device = find_node(usb_name, self.sysfs, self.hid_sysfs)
        if name in self.busy:
            raise Refused(f"{usb_name} is in use by another client", retry=True)
        self.busy.add(name)
        try:
            node = Node(self.opener(name))
            try:
                await self._serve_node(node, usb_name, device, uid, reader, writer)
            finally:
                node.close()
        finally:
            self.busy.discard(name)

    async def _serve_node(self, node: Node, usb_name: str, device: int, uid: int, reader, writer) -> None:
        saved = unit = None
        if device == policy.KEYBOARD:  # behind a receiver, whose notifications need turning on
            unit = await read_paired_unit(node)
            saved = await read_flags(node)
            if saved is None:
                log.warning("%s: can't read the receiver's notification flags", usb_name)
            elif not await write_flags(node, NOTIFY_FLAGS):
                log.warning("%s: can't turn on the receiver's notifications", usb_name)
        log.info("%s: opened for uid %d%s", usb_name, uid, f", paired keyboard {unit}" if unit else "")
        session = Session(node, writer, usb_name, device)
        node.listener = session.from_node
        writer.write(wire.answer(True, unit=unit))
        session.discover()
        try:
            await self._pump(node, session, reader, writer)
        finally:
            node.listener = lambda _r: None
            await session.stop_discovery()
            await self._restore(node, session.rules, saved)
            log.info("%s: closed", usb_name)

    async def _pump(self, node: Node, session: Session, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frames = wire.FrameReader()
        tokens, last = float(self.burst), time.monotonic()
        lost = asyncio.ensure_future(node.lost.wait())
        try:
            while True:
                read = asyncio.ensure_future(reader.read(4096))
                done, _ = await asyncio.wait({read, lost}, return_when=asyncio.FIRST_COMPLETED)
                if lost in done:
                    read.cancel()
                    return
                data = read.result()
                if not data:
                    return
                for report in frames.feed(data):  # WireError ends the session
                    now = time.monotonic()
                    tokens = min(self.burst, tokens + (now - last) * self.rate)
                    last = now
                    if tokens < 1:
                        await asyncio.sleep((1 - tokens) / self.rate)
                        tokens, last = 1.0, time.monotonic()
                    tokens -= 1
                    session.from_client(report)
                await writer.drain()
        finally:
            lost.cancel()

    async def _restore(self, node: Node, rules: Policy, saved: tuple[int, int, int] | None) -> None:
        for feature, function, params in rules.restore_requests():
            index = rules.index_of(feature)
            report = policy.build_request(index, function, policy.RESTORE_SW, params, rules.device)
            reply = await node.transact(
                report, lambda r, i=index, f=function: policy.is_reply(r, i, f, policy.RESTORE_SW, rules.device))
            if reply is None:
                break  # asleep or gone: it returns to onboard mode on its next power cycle anyway
        if saved is not None:
            await write_flags(node, saved)


# -- entry point -----------------------------------------------------------------------------


def listening_socket(path: str | None) -> socket.socket:
    """systemd's socket (socket activation), else one bound at ``path``."""
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS", "0")) >= 1:
        return socket.socket(fileno=3)
    if not path:
        sys.exit("not socket-activated; pass --socket PATH")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    os.chmod(path, 0o666)  # access is decided per connection (SO_PEERCRED)
    sock.listen()
    return sock


def main() -> None:
    ap = argparse.ArgumentParser(prog="borochid-hidpp-broker", description=__doc__.split("\n\n")[0])
    ap.add_argument("--socket", help="listen here instead of on systemd's socket")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    sock = listening_socket(args.socket)
    sock.setblocking(False)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Broker().serve(sock))


if __name__ == "__main__":
    main()

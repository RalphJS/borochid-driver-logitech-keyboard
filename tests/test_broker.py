"""The broker end to end: a real Unix socket, a fake sysfs tree and a fake
keyboard on a socketpair standing in for the hidraw node."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from pathlib import Path

import pytest

from borochid_logitech_keyboard import wire
from borochid_logitech_keyboard.broker import server
from borochid_logitech_keyboard.broker.server import Broker, Refused, active_uid, find_node

from borochid_logitech_keyboard.broker import policy

from test_policy import G915, keyboard_reply, req

SW = 0x0B


def make_sysfs(root: Path, vendor: str = "046d", product: str = "c541", with_node: bool = True) -> Path:
    usb = root / "1-3"
    usb.mkdir(parents=True)
    (usb / "idVendor").write_text(vendor + "\n")
    (usb / "idProduct").write_text(product + "\n")
    for n in (0, 1, 2):
        iface = usb / f"1-3:1.{n}"
        iface.mkdir()
        (iface / "bInterfaceNumber").write_text(f"{n:02x}\n")
        if with_node or n != 2:
            (iface / f"0003:046D:C541.000{n + 2}" / "hidraw" / f"hidraw{n + 1}").mkdir(parents=True)
    return root


class FakeKeyboard:
    """The receiver's HID++ interface with a G915 behind it (``device`` 1),
    or the G915's own on its cable (``device`` 0xFF, no receiver)."""

    def __init__(self, fd: int, device: int = 0x01):
        self.fd = fd
        self.device = device
        self.flags = (0, 0, 0)
        self.asleep = False
        self.requests: list[bytes] = []
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            r = os.read(self.fd, 64)
        except OSError:
            asyncio.get_running_loop().remove_reader(self.fd)
            return
        if not r:
            asyncio.get_running_loop().remove_reader(self.fd)
            return
        self.requests.append(r)
        if r[0] == 0x10 and r[1] == 0xFF and self.device == 0x01:
            if r[2] == 0x83 and r[3] == 0xB5 and r[4] == 0x30:  # pairing record, slot 1
                self.send(bytes([0x11, 0xFF, 0x83, 0xB5, 0x30, 0x94, 0x54, 0xDC, 0xB7]).ljust(20, b"\0"))
            elif r[2] == 0x81 and r[3] == 0x00:
                self.send(bytes([0x10, 0xFF, 0x81, 0x00, *self.flags]))
            elif r[2] == 0x80 and r[3] == 0x00:
                self.flags = tuple(r[4:7])
                self.send(bytes([0x10, 0xFF, 0x80, 0x00, 0, 0, 0]))
            return
        if self.asleep or r[0] != 0x11 or r[1] != self.device:
            return
        if (r[2] == 0 and r[3] >> 4 == 0) or r[2] == G915[0x0001]:
            self.send(keyboard_reply(r, device=self.device))
        else:
            self.send(bytes([0x11, self.device, r[2], r[3]]).ljust(20, b"\0"))

    def send(self, report: bytes) -> None:
        os.write(self.fd, report)

    def device_requests(self) -> list[bytes]:
        return [r for r in self.requests if r[0] == 0x11]

    def from_client(self) -> list[bytes]:
        return [r for r in self.device_requests() if r[3] & 0x0F not in policy.RESERVED_SW]

    def discovery_rounds(self) -> int:
        return sum(1 for r in self.device_requests() if r[2] == 0 and r[3] & 0x0F in policy.DISCOVERY_SW)


BT_ADDRESS = "ef:24:28:5f:95:8a"


def make_hid_sysfs(root: Path, product: str = "B354", address: str = BT_ADDRESS) -> Path:
    """/sys/bus/hid/devices with the G915 over Bluetooth (a uhid device)."""
    hid = root / f"0005:046D:{product}.003C"
    (hid / "hidraw" / "hidraw1").mkdir(parents=True)
    (hid / "uevent").write_text(f"HID_ID=0005:0000046D:0000{product}\nHID_NAME=G915 KEYBOARD\nHID_UNIQ={address}\n")
    usb = root / "0003:046D:C33E.0002"  # a USB HID device: not looked at for Bluetooth
    (usb / "hidraw" / "hidraw9").mkdir(parents=True)
    (usb / "uevent").write_text(f"HID_ID=0003:0000046D:0000C33E\nHID_UNIQ={address}\n")
    return root


@contextlib.asynccontextmanager
async def make_env(tmp_path, product: str = "c541", device: int = 0x01):
    sysfs = make_sysfs(tmp_path / "usb", product=product)
    hid_sysfs = make_hid_sysfs(tmp_path / "hid")
    seat = tmp_path / "seat0"
    seat.write_text(f"IS_SEAT0=1\nACTIVE=c2\nACTIVE_UID={os.getuid()}\n")
    node_end, kb_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    node_end.setblocking(False)
    kb_end.setblocking(False)
    opened: list[str] = []

    def opener(name: str) -> int:
        opened.append(name)
        return os.dup(node_end.fileno())

    path = str(tmp_path / "broker.sock")
    sock = server.listening_socket(path)
    sock.setblocking(False)
    broker = Broker(sysfs=sysfs, seat_file=seat, opener=opener, hid_sysfs=hid_sysfs)
    task = asyncio.ensure_future(broker.serve(sock))
    kb = FakeKeyboard(kb_end.fileno(), device)
    try:
        yield {"path": path, "kb": kb, "broker": broker, "seat": seat, "opened": opened, "sysfs": sysfs, "kb_sock": kb_end}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(OSError, ValueError):
            asyncio.get_running_loop().remove_reader(kb_end.fileno())
        node_end.close()
        kb_end.close()


def with_env(fn, **env_args):
    """Run an async scenario against a fresh broker (no pytest-asyncio here)."""

    def test(tmp_path):
        async def main():
            async with make_env(tmp_path, **env_args) as env:
                await fn(env)

        asyncio.run(asyncio.wait_for(main(), 10))

    test.__name__ = fn.__name__
    return test


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.frames = wire.FrameReader()
        self.inbox: list[bytes] = []

    @classmethod
    async def connect(cls, path: str, usb: str = "1-3") -> tuple[Client, tuple[bool, str | None, bool]]:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(wire.hello(usb))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 2)
        client = cls(reader, writer)
        client.unit = wire.answer_unit(line)
        return client, wire.parse_answer(line)

    async def send(self, report: bytes) -> None:
        self.writer.write(wire.frame(report))
        await self.writer.drain()

    async def recv(self, timeout: float = 1.0) -> bytes | None:
        deadline = time.monotonic() + timeout
        while not self.inbox:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                data = await asyncio.wait_for(self.reader.read(4096), left)
            except TimeoutError:
                return None
            if not data:
                return None
            self.inbox += self.frames.feed(data)
        return self.inbox.pop(0)

    async def call(self, report: bytes) -> bytes | None:
        await self.send(report)
        return await self.recv()

    async def learn(self, feature: int) -> int:
        r = await self.call(req(0, 0, feature >> 8, feature & 0xFF))
        return r[4]

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


async def settle(cond, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


# -- node lookup and peers ------------------------------------------------------------


def with_wired_env(fn):
    """The same, with the G915 on its USB cable instead of the receiver."""
    return with_env(fn, product="c33e", device=0xFF)


def test_find_node_picks_the_hidpp_interface(tmp_path):
    assert find_node("1-3", make_sysfs(tmp_path)) == ("hidraw3", policy.KEYBOARD)
    assert find_node("1-3", make_sysfs(tmp_path / "wired", product="c33e")) == ("hidraw3", policy.WIRED)


def test_find_node_refuses_other_devices(tmp_path):
    with pytest.raises(Refused) as e:
        find_node("1-3", make_sysfs(tmp_path, product="c547"))
    assert not e.value.retry
    with pytest.raises(Refused) as e:
        find_node("1-3", make_sysfs(tmp_path / "b", vendor="1b1c"))
    assert not e.value.retry


def test_find_node_retries_while_missing(tmp_path):
    with pytest.raises(Refused) as e:
        find_node("1-3", make_sysfs(tmp_path, with_node=False))
    assert e.value.retry
    with pytest.raises(Refused) as e:
        find_node("9-9", tmp_path)
    assert e.value.retry


def test_open_hidraw_only_opens_hidraw_names(tmp_path):
    for bad in ("../etc/shadow", "hidraw1/../../x", "sda", "hidraw"):
        with pytest.raises(Refused):
            server.open_hidraw(bad, tmp_path)


def test_active_uid(tmp_path):
    seat = tmp_path / "seat0"
    seat.write_text("ACTIVE=c1\nACTIVE_UID=1000\n")
    assert active_uid(seat) == 1000
    seat.write_text("ACTIVE=\n")
    assert active_uid(seat) is None
    assert active_uid(tmp_path / "missing") is None


@with_env
async def test_other_users_refused(env):
    env["seat"].write_text(f"ACTIVE_UID={os.getuid() + 1}\n")
    if os.getuid() == 0:
        pytest.skip("root is always allowed")
    client, (ok, error, retry) = await Client.connect(env["path"])
    assert not ok and "not the active session" in error and not retry
    assert env["opened"] == []
    await client.close()


@with_env
async def test_garbage_hello_refused(env):
    reader, writer = await asyncio.open_unix_connection(env["path"])
    writer.write(b'{"v": 1, "open": "../../x"}\n')
    ok, error, _ = wire.parse_answer(await reader.readline())
    assert not ok and env["opened"] == []
    writer.close()


# -- a session ------------------------------------------------------------------------------


@with_env
async def test_open_sets_and_restores_receiver_flags(env):
    kb = env["kb"]
    kb.flags = (0, 0, 0)
    client, (ok, _, _) = await Client.connect(env["path"])
    assert ok and env["opened"] == ["hidraw3"]
    assert kb.flags == (0x00, 0x09, 0x00)
    await client.close()
    await settle(lambda: kb.flags == (0, 0, 0))


@with_env
async def test_requests_go_to_the_keyboard_and_replies_come_back(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    index = await client.learn(0x8040)
    assert index == G915[0x8040]
    r = await client.call(req(index, 1))
    assert r[2] == index and r[3] == (1 << 4 | SW)
    assert all(q[1] == 0x01 for q in kb.device_requests())  # always the keyboard's index
    assert not any(q[2] == 0 and q[3] >> 4 == 0 for q in kb.from_client())  # ROOT answered by the broker
    await client.close()


@with_env
async def test_refused_requests_never_reach_the_keyboard(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    await client.learn(0x8040)  # discovery done
    before = len(kb.device_requests())
    r = await client.call(req(0, 0, 0x1B, 0xC0))  # ask for the keylogging feature
    assert r[2] == 0 and r[4:7] == b"\0\0\0"  # "not supported", its index never revealed
    r = await client.call(req(G915[0x1BC0], 1, 1))  # or guess its index
    assert r[2] == 0xFF and r[5] == 0x09
    r = await client.call(req(0, 1, 0, 0, 0x5A, sw=policy.DISCOVERY_SW[0]))  # or pose as the broker
    assert r[2] == 0xFF
    assert len(kb.device_requests()) == before
    await client.close()


@with_env
async def test_only_allowed_notifications_come_back(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    g = await client.learn(0x8010)
    await client.learn(0x8071)
    kb.send(bytes([0x11, 0x01, G915[0x8071], 0x10]).ljust(20, b"\0"))  # activity, not forwarded
    kb.send(bytes([0x11, 0x01, G915[0x1BC0], 0x00, 0, 0x04]).ljust(20, b"\0"))
    kb.send(bytes([0x11, 0x01, g, 0x00, 0x01]).ljust(20, b"\0"))  # G1 down
    r = await client.recv()
    assert r[2] == g and r[4] == 0x01
    assert await client.recv(0.2) is None
    await client.close()


@with_env
async def test_disconnect_hands_the_keyboard_back(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    for feature in (0x8010, 0x8071, 0x8100):
        await client.learn(feature)
    await client.close()
    await settle(lambda: kb.flags == (0, 0, 0))
    restores = [q for q in kb.device_requests() if q[3] & 0x0F == policy.RESTORE_SW]
    # Discovery found every feature, so the whole hand-back runs, onboard mode last.
    assert [(q[2], q[3] >> 4, q[4]) for q in restores] == [
        (G915[0x8010], 2, 0), (G915[0x8030], 0, 0), (G915[0x8020], 1, 0),
        (G915[0x8071], 5, 1), (G915[0x4522], 3, 0), (G915[0x8100], 1, 1),
    ]  # fmt: skip


@with_env
async def test_restore_stops_when_the_keyboard_sleeps(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    for feature in (0x8010, 0x8071, 0x8100):
        await client.learn(feature)
    kb.asleep = True
    await client.close()
    await settle(lambda: kb.flags == (0, 0, 0))
    restores = [q for q in kb.device_requests() if q[3] & 0x0F == policy.RESTORE_SW]
    assert len(restores) == 1


@with_env
async def test_second_client_is_busy_until_the_first_leaves(env):
    first, (ok, _, _) = await Client.connect(env["path"])
    assert ok
    second, (ok, error, retry) = await Client.connect(env["path"])
    assert not ok and retry and "in use" in error
    await second.close()
    await first.close()
    await settle(lambda: not env["broker"].busy)
    third, (ok, _, _) = await Client.connect(env["path"])
    assert ok
    await third.close()


@with_env
async def test_bad_frame_ends_the_session(env):
    client, _ = await Client.connect(env["path"])
    client.writer.write(b"\x00junk")
    await client.writer.drain()
    assert await client.recv(1.0) is None  # closed
    await settle(lambda: not env["broker"].busy)
    await client.close()


@with_env
async def test_lost_node_ends_the_session(env):
    client, _ = await Client.connect(env["path"])
    await client.learn(0x8040)
    # Unplugging the receiver: the node's peer goes away.
    env["kb_sock"].shutdown(socket.SHUT_RDWR)
    assert await client.recv(1.0) is None
    await settle(lambda: not env["broker"].busy)
    await client.close()


@with_env
async def test_requests_are_rate_limited(env):
    env["broker"].rate, env["broker"].burst = 50.0, 2
    client, _ = await Client.connect(env["path"])
    start = time.monotonic()
    for _ in range(10):
        await client.send(req(0, 1, 0, 0, 0x5A))
    for _ in range(10):
        assert await client.recv(2.0) is not None
    assert time.monotonic() - start >= (10 - 2) / 50 * 0.9
    await client.close()


@with_env
async def test_root_query_waits_for_discovery_and_carries_the_clients_sw(env):
    kb = env["kb"]
    kb.asleep = True  # discovery at open times out
    client, _ = await Client.connect(env["path"])
    await settle(lambda: kb.discovery_rounds() == 1)
    await asyncio.sleep(server.REPLY_TIMEOUT + 0.1)
    kb.asleep = False
    server.DISCOVERY_SPACING, spacing = 0.0, server.DISCOVERY_SPACING
    try:
        r = await client.call(req(0, 0, 0x80, 0x40, sw=0x0A))  # starts a new round, answered when it's done
    finally:
        server.DISCOVERY_SPACING = spacing
    assert r[:4] == bytes([0x11, 0x01, 0x00, 0x0A]) and r[4] == G915[0x8040]
    assert kb.discovery_rounds() == 2
    await client.close()


@with_env
async def test_a_deferred_answer_is_dropped_once_the_client_moves_on(env):
    kb = env["kb"]
    kb.asleep = True
    client, _ = await Client.connect(env["path"])
    await settle(lambda: kb.discovery_rounds() == 1)
    await client.send(req(0, 0, 0x80, 0x40))  # deferred: the round in flight will fail
    await client.send(req(0, 1, 0, 0, 0x5A))  # the client gave up and moved on
    assert await client.recv(server.REPLY_TIMEOUT * 3) is None  # nothing answered late
    await client.close()


@with_env
async def test_failed_discovery_retries_when_the_keyboard_shows_life(env):
    kb = env["kb"]
    kb.asleep = True
    server.DISCOVERY_SPACING, spacing = 0.2, server.DISCOVERY_SPACING
    try:
        client, _ = await Client.connect(env["path"])
        await settle(lambda: kb.discovery_rounds() == 1)
        await asyncio.sleep(server.REPLY_TIMEOUT + 0.05)
        kb.asleep = False
        kb.send(bytes([0x10, 0x01, 0x41, 0x04, 0x7C, 0x40, 0]))  # it wakes and reconnects
        assert (await client.recv())[2] == 0x41  # the link notification reaches the client
        await settle(lambda: kb.discovery_rounds() == 2)
        r = await client.call(req(0, 0, 0x80, 0x10))
        assert r[4] == G915[0x8010]
        # Rounds rotate software ids, so a stale reply from round 1 can't answer round 2.
        sws = [q[3] & 0x0F for q in kb.device_requests() if q[2] == 0 and q[3] & 0x0F in policy.DISCOVERY_SW]
        assert sws == [0x0C, 0x0D]
        # More signs of life once the table is complete start nothing new.
        kb.send(bytes([0x11, 0x01, G915[0x8040], 0x00, 0, 50]).ljust(20, b"\0"))
        await asyncio.sleep(0.3)
        assert kb.discovery_rounds() == 2
        await client.close()
    finally:
        server.DISCOVERY_SPACING = spacing


@with_env
async def test_rounds_are_spaced(env):
    kb = env["kb"]
    kb.asleep = True
    client, _ = await Client.connect(env["path"])
    await settle(lambda: kb.discovery_rounds() == 1)
    await asyncio.sleep(server.REPLY_TIMEOUT + 0.05)
    kb.asleep = False
    for _ in range(5):
        kb.send(bytes([0x10, 0x01, 0x41, 0x04, 0x7C, 0x40, 0]))
        await asyncio.sleep(0.02)
    assert kb.discovery_rounds() == 1  # within DISCOVERY_SPACING of the first
    await client.close()


@with_wired_env
async def test_on_a_cable_the_keyboard_is_the_device_and_there_is_no_receiver(env):
    kb = env["kb"]
    client, (ok, _, _) = await Client.connect(env["path"])
    assert ok
    index = await client.learn(0x8040)
    r = await client.call(req(index, 1))
    assert r[1] == 0xFF and r[2] == index
    assert all(q[1] == 0xFF for q in kb.device_requests())  # always the keyboard itself
    assert not [q for q in kb.requests if q[0] == 0x10]  # no receiver registers touched
    await client.close()


@with_wired_env
async def test_on_a_cable_disconnect_still_hands_the_keyboard_back(env):
    kb = env["kb"]
    client, _ = await Client.connect(env["path"])
    await client.learn(0x8100)
    await client.close()
    await settle(lambda: any(q[3] & 0x0F == policy.RESTORE_SW and q[2] == G915[0x8100] for q in kb.device_requests()))
    restores = [q for q in kb.device_requests() if q[3] & 0x0F == policy.RESTORE_SW]
    assert restores and all(q[1] == 0xFF for q in restores)


@with_env
async def test_a_receiver_tells_the_client_its_keyboards_unit_id(env):
    env["kb"].asleep = True  # even with the keyboard away: the receiver's pairing record
    client, (ok, _, _) = await Client.connect(env["path"])
    assert ok and client.unit == "9454DCB7"
    await client.close()


@with_wired_env
async def test_on_a_cable_there_is_no_pairing_record_to_read(env):
    client, (ok, _, _) = await Client.connect(env["path"])
    assert ok and client.unit is None  # the driver reads the unit ID from the keyboard itself
    await client.close()


def test_only_a_well_formed_unit_id_is_taken_from_the_answer():
    assert wire.answer_unit(wire.answer(True, unit="9454DCB7")) == "9454DCB7"
    for bad in (b'{"ok": true, "unit": "../../x"}\n', b'{"ok": true, "unit": 5}\n', b"[]\n", b"garbage\n"):
        assert wire.answer_unit(bad) is None
    assert "unit" not in wire.answer(False, "no", unit="9454DCB7").decode()


# -- over Bluetooth --------------------------------------------------------------------


def test_find_node_finds_the_keyboard_over_bluetooth_by_address(tmp_path):
    hid = make_hid_sysfs(tmp_path)
    assert find_node(f"bt:{BT_ADDRESS}", hid_sysfs=hid) == ("hidraw1", policy.WIRED)
    with pytest.raises(Refused) as e:  # not connected (or another keyboard): try again later
        find_node("bt:00:11:22:33:44:55", hid_sysfs=hid)
    assert e.value.retry
    with pytest.raises(Refused):  # a Bluetooth HID device that isn't a supported keyboard
        find_node(f"bt:{BT_ADDRESS}", hid_sysfs=make_hid_sysfs(tmp_path / "other", product="B019"))


def test_hello_takes_a_usb_name_or_a_bluetooth_address():
    assert wire.parse_hello(wire.hello(f"bt:{BT_ADDRESS}")) == f"bt:{BT_ADDRESS}"
    for bad in ("bt:../../etc", "bt:EF:24:28:5F:95:8A", "bt:ef:24:28:5f:95", "bluetooth", "1-3/../x"):
        with pytest.raises(wire.WireError):
            wire.hello(bad)


@with_wired_env
async def test_over_bluetooth_only_hidpp_reaches_the_client_never_keystrokes(env):
    kb = env["kb"]
    client, (ok, _, _) = await Client.connect(env["path"], usb=f"bt:{BT_ADDRESS}")
    assert ok and env["opened"] == ["hidraw1"] and client.unit is None
    index = await client.learn(0x8040)
    kb.send(bytes([0x01, 0x00, 0x00, 0x04, 0, 0, 0, 0, 0]))  # keyboard report: "a" down
    kb.send(bytes([0x03, 0xE9, 0x00]))  # consumer report: volume up
    r = await client.call(req(index, 1))
    assert r[0] == 0x11 and r[1] == 0xFF and r[2] == index
    assert await client.recv(0.2) is None  # the keystrokes never came through
    await client.close()

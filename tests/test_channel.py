import asyncio

import pytest

from borochid.common.models import Bus, DeviceIdentity

from borochid_logitech_keyboard import wire
from borochid_logitech_keyboard.channel import NOT_INSTALLED, BrokerChannel, NoLink

IDENT = DeviceIdentity(Bus.USB, "usb:1-3", vid=0x046D, pid=0xC541, attrs={"sys_path": "/sys/devices/pci0/usb1/1-3"})


def channel(path) -> tuple[BrokerChannel, list, list]:
    ch = BrokerChannel(IDENT, {})
    ch.socket_path = str(path)
    ch.retry_s = (0.01,)
    links, data = [], []
    ch.on_link = lambda up, problem: links.append((up, problem))
    ch.on_data = data.append
    return ch, links, data


async def until(cond, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.005)


def test_opens_the_receiver_and_carries_frames(tmp_path, run):
    async def main():
        seen = {}

        async def broker(reader, writer):
            seen["hello"] = wire.parse_hello(await reader.readline())
            writer.write(wire.answer(True) + wire.frame(b"\x11\x01\x0a\x1b") + wire.frame(b"\x10\x01\x41\x00"))
            await writer.drain()
            frames = wire.FrameReader()
            seen["got"] = frames.feed(await reader.read(64))
            writer.close()

        server = await asyncio.start_unix_server(broker, tmp_path / "s")
        ch, links, data = channel(tmp_path / "s")
        await ch.open()
        await until(lambda: len(data) == 2)
        await ch.write(b"\x11\xff\x00\x1b")
        await until(lambda: "got" in seen)
        await ch.close()
        server.close()
        return seen, links, data

    seen, links, data = run(main())
    assert seen["hello"] == "1-3" and seen["got"] == [b"\x11\xff\x00\x1b"]
    assert data == [b"\x11\x01\x0a\x1b", b"\x10\x01\x41\x00"]
    assert links[0] == (True, None)


def test_waits_for_a_broker_that_isnt_there_yet(tmp_path, run):
    async def main():
        ch, links, _ = channel(tmp_path / "s")
        await ch.open()
        await until(lambda: links)
        with pytest.raises(NoLink):
            await ch.write(b"\x11\xff\x00\x1b")

        async def broker(reader, writer):
            await reader.readline()
            writer.write(wire.answer(True))
            await writer.drain()
            await reader.read()

        server = await asyncio.start_unix_server(broker, tmp_path / "s")
        await until(lambda: ch.up)
        await ch.close()
        server.close()
        return links

    links = run(main())
    assert links[0] == (False, NOT_INSTALLED) and links[-1] == (True, None)


def test_refusal_is_shown_and_retried(tmp_path, run):
    async def main():
        answers = [wire.answer(False, "device not ready", retry=True), wire.answer(True)]

        async def broker(reader, writer):
            await reader.readline()
            writer.write(answers.pop(0))
            await writer.drain()
            if not answers:
                await reader.read()
            writer.close()

        server = await asyncio.start_unix_server(broker, tmp_path / "s")
        ch, links, _ = channel(tmp_path / "s")
        await ch.open()
        await until(lambda: ch.up)
        await ch.close()
        server.close()
        return links

    links = run(main())
    assert links[0][0] is False and "device not ready" in links[0][1]
    assert links[-1] == (True, None)


def test_broker_restart_reconnects(tmp_path, run):
    async def main():
        conns = []

        async def broker(reader, writer):
            await reader.readline()
            writer.write(wire.answer(True))
            await writer.drain()
            conns.append(writer)
            if len(conns) == 1:
                writer.close()  # the broker restarts
            else:
                await reader.read()

        server = await asyncio.start_unix_server(broker, tmp_path / "s")
        ch, links, _ = channel(tmp_path / "s")
        await ch.open()
        await until(lambda: len(conns) == 2 and ch.up)
        await ch.close()
        server.close()
        return links

    ups = [up for up, _ in run(main())]
    assert ups[:3] == [True, False, True]


def test_the_broker_is_asked_for_a_usb_device_or_a_bluetooth_address():
    assert BrokerChannel(IDENT, {})._device_name() == "1-3"
    ble = DeviceIdentity(Bus.BLE, "ble:EF:24:28:5F:95:8A", name="G915 KEYBOARD", attrs={"address": "EF:24:28:5F:95:8A"})
    assert BrokerChannel(ble, {})._device_name() == "bt:ef:24:28:5f:95:8a"

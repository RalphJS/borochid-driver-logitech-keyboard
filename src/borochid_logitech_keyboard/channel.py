"""A Borochid channel to a keyboard through the HID++ broker.

The G915's receiver node is closed to the session (see the broker), so
this channel talks to the broker's socket instead of opening hidraw. It
names the receiver by its USB device (from the detector), and from then on
carries HID++ reports both ways (see ``wire.py``).

The broker may start after the service, restart, or refuse for a moment
while udev sets the node up. None of that is fatal: the channel keeps
reconnecting and tells the driver through ``on_link(up, problem)``, so the
device stays listed and the driver shows why it's waiting. ``on_closed``
(which drops the device) is left to the detector, when the receiver goes
away.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from borochid.common.models import Bus
from borochid.service.channels import Channel, ChannelError

from borochid_logitech_keyboard import wire

log = logging.getLogger(__name__)

NOT_INSTALLED = (
    "the Borochid HID++ broker isn't running "
    "(install borochid-driver-logitech-keyboard and enable borochid-hidpp-broker.socket)"
)


class NoLink(ChannelError):
    """Not connected to the broker right now."""


class BrokerChannel(Channel):
    # Never from the manifest: packages don't pick sockets. The environment
    # (the user's own service) may, for a development broker.
    socket_path = os.environ.get("BOROCHID_HIDPP_BROKER_SOCKET") or wire.SOCKET_PATH
    retry_s = (1.0, 2.0, 5.0, 10.0)

    def __init__(self, ident, spec):
        super().__init__(ident, spec)
        self.on_link: Callable[[bool, str | None], None] = lambda _up, _problem: None
        self.problem: str | None = "connecting to the HID++ broker"
        self.unit_id: str | None = None  # the paired keyboard's, from the broker (receivers only)
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    def _device_name(self) -> str:
        """What the broker opens: the USB device, or ``bt:<address>`` for a
        keyboard the Bluetooth detector found."""
        if self.ident.bus is Bus.BLE:
            return "bt:" + str(self.ident.attrs.get("address", "")).lower()
        return Path(self.ident.attrs["sys_path"]).name

    @property
    def up(self) -> bool:
        return self._writer is not None

    async def open(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except (OSError, wire.WireError, _Refused) as e:
                self._set_down(str(e) if not isinstance(e, (FileNotFoundError, ConnectionRefusedError)) else NOT_INSTALLED)
            await asyncio.sleep(self.retry_s[min(attempt, len(self.retry_s) - 1)])
            attempt += 1

    async def _session(self) -> None:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write(wire.hello(self._device_name()))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 5)
            ok, error, retry = wire.parse_answer(line)
            if not ok:
                raise _Refused(f"the HID++ broker refused: {error}" + ("" if retry else " (not retrying soon)"))
            self.unit_id = wire.answer_unit(line) or self.unit_id
            self._writer = writer
            self.problem = None
            self.on_link(True, None)
            frames = wire.FrameReader()
            while data := await reader.read(4096):
                for report in frames.feed(data):
                    self._deliver(report)
            self._set_down("the HID++ broker closed the connection")
        finally:
            self._writer = None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _set_down(self, problem: str) -> None:
        self._writer = None
        if problem != self.problem:
            log.info("%s: %s", self.ident.uid, problem)
        self.problem = problem
        self.on_link(False, problem)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise NoLink(self.problem or "not connected")
        self._writer.write(wire.frame(data))
        await self._writer.drain()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None


class _Refused(Exception):
    pass

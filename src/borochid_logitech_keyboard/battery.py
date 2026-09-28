"""Battery level from BATTERY_VOLTAGE (0x1001). Pure: no I/O.

The G915 reports its cell voltage, not a percentage, and the receiver it
uses isn't handled by the kernel's HID++ driver, so there is no
power_supply to ask: this driver turns the voltage into a level itself.

Reply and notification: ``[voltage hi, voltage lo, flags]``. Flags bit 7 is
set while external power is connected; bits 0-1 then say how charging goes
(0 charging, 1 full, 2 not charging), per Solaar's reading of the feature.
"""

from __future__ import annotations

from dataclasses import dataclass

# (millivolts, percent) for a single Li-ion cell under light load, falling.
# The discharge curve Solaar uses for BATTERY_VOLTAGE devices. An estimate:
# the keyboard doesn't say what its own gauge thinks.
CURVE = (
    (4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50),
    (3778, 40), (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0),
)  # fmt: skip


@dataclass(frozen=True)
class Reading:
    millivolts: int
    level: int
    charging: bool
    full: bool


def level(millivolts: int) -> int:
    if millivolts >= CURVE[0][0]:
        return 100
    for (v_hi, p_hi), (v_lo, p_lo) in zip(CURVE, CURVE[1:], strict=False):
        if millivolts >= v_lo:
            return round(p_lo + (p_hi - p_lo) * (millivolts - v_lo) / (v_hi - v_lo))
    return 0


def parse(params: bytes) -> Reading:
    mv = int.from_bytes(params[0:2], "big")
    flags = params[2] if len(params) > 2 else 0
    external = bool(flags & 0x80)
    status = flags & 0x03
    return Reading(mv, level(mv), charging=external and status == 0, full=external and status == 1)

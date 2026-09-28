# borochid-driver-logitech-keyboard

[Borochid](../borochid) driver for Logitech keyboards that speak **HID++
2.0** (first model: G915 WIRELESS), and the **HID++ broker** it talks
through.

This repo is **code only**. Keyboard models are described by signed data
packages (for example [`borochid-logitech-g915`](../borochid-logitech-g915)):
the key map (positions, lighting zone ids, HID usages), the G-keys, the
number of M-key subprofiles and the starting settings.

## What it does

While the service runs, the keyboard is in **host mode**, and the driver
applies the active Borochid profile. Profiles are Borochid's, shared by
every device; the keyboard's settings for each live in the service's
settings store, never in the keyboard.

* **Subprofiles (M1-M3)**: like G HUB, each profile has one per M-key,
  each with its own G-key bindings and lighting. The M-key LEDs show which
  is active.
* **G1-G5**: a key chord or wheel step (replayed through the service's
  virtual input device while the key is held), or nothing.
* **Lighting**: per-key colours on a base colour, or a whole-keyboard
  effect (breathing, colour cycle, wave, ripple) with colour and speed, or
  off. The logo follows.
* **Game mode**: the game-mode key and its LED stay the keyboard's; the
  profile picks which keys it disables. The keyboard always disables both
  Super keys in game mode by itself.
* **Brightness** (also stepped by the brightness key, and saved with the
  profile) and **report rate**.
* **Battery**: from the cell voltage.
* **MR** does nothing (outside X mode): recording a macro means reading
  keystrokes, which Borochid never does.
* **X mode** (per profile): no subprofiles, M1's bindings and lighting
  apply, and M1-M3 and MR replay their own bindings like G-keys. Their
  LEDs stay lit while it is on.
* **Num Lock light** (per profile, per-key colours only): the Num Lock key
  is dark while Num Lock is off. The driver reads the Num Lock LED the
  kernel keeps for the receiver's keyboard (sysfs), not keystrokes.

**Nothing is written to the keyboard's memory.** No onboard profiles, no
boot lighting, no flash: everything is RAM state that a power cycle clears.
When the service stops (or dies), the keyboard goes back to its onboard
profile.

## Why a broker

The G915's receiver (`046d:c541`) isn't handled by `hid-logitech-dj`, so
there is no per-keyboard HID node; the HID++ traffic is on the receiver's
interface 2 (reports `0x10`/`0x11` only; keystrokes use interfaces 0 and 1).
But the keyboard has feature **`0x1BC0` REPORT_HID_USAGE**, which makes it
report **every key press over HID++**. Any program that can write HID++ to
that node could turn it on and read everything typed, so the node must not
be given to the session user (`uaccess`), as the mouse driver does.

Instead:

* The same goes for the keyboard's other connections: on its **USB cable**
  (`046d:c33e`, the keyboard itself, HID++ on interface 2) and over
  **Bluetooth** (`046d:b354`, BlueZ's HID-over-GATT device). Over Bluetooth
  one HID node carries the keystrokes *and* HID++, which makes keeping it
  away from the session even more important; the broker passes on HID++
  reports only (`0x10`/`0x11` addressed to the keyboard) and drops the rest.
  Behind the receiver the keyboard is device 1; on the cable and over
  Bluetooth it is the device itself (`0xFF`).
* udev gives those nodes to the `borochid-hidpp` system user only
  (`packaging/70-borochid-logitech-keyboard.rules`).
* `borochid-hidpp-broker` runs as that user (not root, heavily sandboxed,
  standard library only) and opens it.
* The Borochid service connects to the broker's socket (checked with
  `SO_PEERCRED`: root or the active seat's user) and gets a filtered HID++
  pipe: an allowlist of features, functions and arguments
  (`broker/policy.py`). The broker discovers feature indexes itself, never
  from what the client says, and answers the client's feature lookups from
  its own table, so forbidden features can't even be addressed. Setters
  that would write non-volatile memory are refused. Only replies and a few
  notifications (G/M/MR keys, brightness, battery, link) come back.
* When the client disconnects, the broker hands the keyboard back itself,
  so a crashed service doesn't leave it in host mode.

## Design

| Module | Role |
|---|---|
| `wire.py` | Socket protocol between service and broker. Pure, stdlib only. |
| `broker/policy.py` | What the broker allows, both ways. Pure, stdlib only. |
| `broker/server.py` | The broker daemon. Stdlib only. |
| `channel.py` | Borochid channel to the broker; reconnects on its own. |
| `model.py` | The package's key map and `hidpp_keyboard` section, validated. |
| `settings.py` | Settings per Borochid profile and subprofile. Pure. |
| `lighting.py` | Lighting as HID++ requests. Pure. |
| `battery.py` | Voltage to level. Pure. |
| `driver.py` | Link states, host mode, notifications, actions. |

HID++ framing and the request/reply session come from
`borochid-driver-logitech-hidpp`.

## Measured on a G915 WIRELESS (`407c`, receiver `c541`)

These shaped the design; the tests' `FakeKeyboard` models each of them.

* **Features**: HID++ 4.2; GKEY (5 keys), MKEYS (3), MR,
  BRIGHTNESS_CONTROL (0-100), REPORT_RATE (125/250/500/1000 Hz),
  RGB_EFFECTS (logo zone: off, static, cycle, breathe; key zone: off,
  static, breathe, cycle, wave, ripple, and effect `0x0C`),
  PER_KEY_LIGHTING_V2, DISABLE_KEYS_BY_USAGE (up to 255 keys),
  ONBOARD_PROFILES, BATTERY_VOLTAGE, WIRELESS_DEVICE_STATUS, and
  REPORT_HID_USAGE (`0x1BC0`, see above).
* **The receiver forwards no notifications** until the host sets its HID++
  1.0 register `0x00` to `0x000900` (wireless + software present). The
  broker does, and restores the old value.
* **In host mode, with G-keys diverted** (GKEY fn2), G1-G5 report as a
  bitmask (GKEY fn0), M1-M3 as a bitmask (MKEYS fn0) and MR as 0/1 (MR
  fn0), press and release. The keyboard no longer switches its onboard
  profiles with M-keys.
* **Game mode stays in the firmware.** Its key sends nothing, toggles the
  mode and lights up by itself. DISABLE_KEYS_BY_USAGE sets which keys it
  disables (Caps Lock stopped only while game mode was on); Super is
  disabled in game mode regardless.
* **The brightness key** steps brightness in the keyboard (100 to 50...)
  and notifies the new value (BRIGHTNESS_CONTROL fn0).
* **Per-key zone ids**: HID usage - 3 for the main block, usage - 120 for
  modifiers; brightness 153, play/pause 155, mute 156, next 157, previous
  158, G1-G5 180-184, logo 210. Painting needs host lighting control
  (RGB_EFFECTS SetSWControl 1, 3, 4) and shows on FrameEnd.
* **Battery**: 3835 mV while discharging. The level is estimated from the
  voltage.
* Media keys and the volume roller are ordinary keys and never pass HID++.

## Installing

The driver is an ordinary Borochid driver plugin. The broker needs, once,
as root (packages will do this):

```sh
sudo make install-broker                 # units, sysusers, udev rule
sudo systemd-sysusers
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
sudo systemctl daemon-reload
sudo systemctl enable --now borochid-hidpp-broker.socket
```

`borochid-hidpp-broker` must be on the system (`/usr/bin`), not in a home
directory: the service unit hides `/home`.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ../borochid/packages/common -e ../borochid/packages/service \
    -e ../borochid-driver-logitech-hidpp -e '.[test]'
.venv/bin/pytest
```

A local broker for testing, as yourself, against a node you can open:

```sh
borochid-hidpp-broker --socket /tmp/broker.sock
```

## Adding a model

1. Check the features with `python -m borochid_logitech_hidpp.probe`
   (it needs the node; use the broker's user or a temporary ACL).
2. Add its receiver to `SUPPORTED` in `broker/server.py` and to the udev
   rule, and check `broker/policy.py` covers what it needs.
3. Write the device package: key map (`keyboard`), `hidpp_keyboard`, UI.

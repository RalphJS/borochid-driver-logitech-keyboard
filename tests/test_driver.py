import asyncio

import pytest
from conftest import LEDS, make_driver, settle

from borochid.service.drivers import DriverError
from borochid.service.profiles import Profile

from borochid_logitech_keyboard.driver import NUMLOCK_POLL_S

DEFAULT = Profile("default", "Default")
COPY = {"keys": ["KEY_LEFTCTRL", "KEY_C"]}
BLUE = (0x00, 0xB4, 0xFF)


async def online(settings=None, **spec):
    driver, kb, events, store = make_driver(settings, **spec)
    await driver.start()
    await settle(driver)
    assert driver.state["link"] == "online", driver.state
    return driver, kb, events, store


def test_takes_over_in_host_mode_with_the_default_profile(run):
    driver, kb, _, _ = run(online())
    s = driver.state
    assert kb.mode == 2 and kb.diverted and kb.sw_control == (3, 4)
    assert kb.mr_led == 0 and kb.m_leds == 0b001 and s["subprofile"] == 1
    assert kb.rate_ms == 1 and kb.brightness == 100 and kb.disabled == set()
    # Per-key: static on both zones, then every LED in one frame.
    assert kb.effects[1][0] == 0x01 and kb.effects[0][0] == 0x01
    assert kb.leds == {led: BLUE for led in LEDS}
    assert s["battery"] == 55 and s["charging"] is False and s["online"] is True
    assert s["status"] == "Default · M1"


def test_never_asks_for_features_it_must_not_use(run):
    driver, kb, _, _ = run(online())
    asked = {bytes(p[:2]) for f, fn, p in kb.calls if f == 0 and fn == 0}
    assert b"\x1b\xc0" not in asked  # REPORT_HID_USAGE
    assert b"\x1b\x04" not in asked  # REPROG_CONTROLS


def test_gkey_chord_is_held_as_long_as_the_key(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g2", "binding": COPY})
        kb.press_g(0b10)
        await settle(driver, 1)
        kb.press_g(0)
        await settle(driver, 1)
        return driver

    driver = run(main())
    assert driver.host.input.is_open
    assert driver.host.input.events == [("down", "g2", COPY), ("up", "g2")]


def test_disabled_gkey_does_nothing(run):
    async def main():
        driver, kb, _, _ = await online()
        kb.press_g(0b1)
        await settle(driver, 1)
        return driver

    assert run(main()).host.input.events == []


def test_m_keys_switch_subprofile_bindings_and_lighting(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g1", "binding": COPY})
        kb.press_m(2)
        await settle(driver)
        assert driver.state["subprofile"] == 2 and kb.m_leds == 0b010
        assert driver.state["bind.g1"] == "disabled"  # M2 has its own bindings
        await driver.invoke("set_lighting_color", {"value": "#ff0000"})
        red = dict(kb.leds)
        kb.press_m(1)
        await settle(driver)
        return driver, kb, red

    driver, kb, red = run(main())
    assert set(red.values()) == {(255, 0, 0)}
    assert driver.state["bind.g1"] == COPY and kb.m_leds == 0b001
    assert kb.leds == {led: BLUE for led in LEDS}  # M1's lighting is back


def test_mr_does_nothing(run):
    async def main():
        driver, kb, events, _ = await online()
        before = len(events)
        kb.press_mr()
        await settle(driver, 1)
        return driver, events[before:]

    driver, after = run(main())
    assert after == [] and driver.host.input.events == []


def test_painting_keys_overrides_the_base_colour(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_key_colors", {"keys": [38, "1"], "color": "#FF0000"})
        painted = dict(kb.leds)
        await driver.invoke("set_key_colors", {"keys": [38], "color": None})
        return driver, kb, painted

    driver, kb, painted = run(main())
    assert painted[38] == painted[1] == (255, 0, 0) and painted[180] == BLUE
    assert kb.leds[38] == BLUE and kb.leds[1] == (255, 0, 0)
    assert driver.state["lighting.keys"] == {"1": "#ff0000"}


def test_painting_unknown_keys_is_refused(run):
    async def main():
        driver, _, _, _ = await online()
        with pytest.raises(DriverError, match="no light"):
            await driver.invoke("set_key_colors", {"keys": [99], "color": "#ff0000"})

    run(main())


def test_effects_use_the_keyboards_effect_indexes(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_lighting_mode", {"value": "breathe"})
        breathe = dict(kb.effects)
        await driver.invoke("set_lighting_mode", {"value": "wave"})
        return driver, kb, breathe

    driver, kb, breathe = run(main())
    assert breathe[1][0] == 0x0A and breathe[0][0] == 0x0A and breathe[1][1][:3] == bytes(BLUE)
    assert kb.effects[1][0] == 0x04 and kb.effects[0][0] == 0x03  # the logo has no wave: it cycles
    assert driver.state["lighting.uses_speed"] and not driver.state["lighting.per_key"]


def test_game_mode_keys_are_set_and_super_is_the_firmwares(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_game_mode_keys", {"keys": [57, 41]})
        with pytest.raises(DriverError):
            await driver.invoke("set_game_mode_keys", {"keys": [227]})  # locked: the firmware's
        with pytest.raises(DriverError):
            await driver.invoke("set_game_mode_keys", {"keys": [5]})  # not on this keyboard
        return driver, kb

    driver, kb = run(main())
    assert kb.disabled == {41, 57} and driver.state["game_mode_keys"] == [41, 57]


def test_brightness_key_is_saved_with_the_profile(run):
    async def main():
        driver, kb, _, store = await online()
        kb.brightness_key()
        await settle(driver, 1)
        return driver, store

    driver, store = run(main())
    assert driver.state["brightness"] == 50
    assert store.load()["profiles"]["default"]["brightness"] == 50


def test_profiles_keep_their_own_settings(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.use_profile(Profile("p1", "Games", copy_of="default"), {"default", "p1"})
        await driver.invoke("set_report_rate", {"value": 500})
        await driver.invoke("set_game_mode_keys", {"keys": [4]})
        rate_games = kb.rate_ms
        await driver.use_profile(DEFAULT, {"default", "p1"})
        return driver, kb, rate_games

    driver, kb, rate_games = run(main())
    assert rate_games == 2 and kb.rate_ms == 1 and kb.disabled == set()
    assert driver.state["status"] == "Default · M1"


def test_sleep_saves_changes_and_applies_them_on_wake(run):
    async def main():
        driver, kb, _, _ = await online()
        kb.asleep = True
        await driver.invoke("set_lighting_color", {"value": "#00ff00"})
        assert driver.state["link"] == "asleep"
        kb.link(True)
        await settle(driver)
        return driver, kb

    driver, kb = run(main())
    assert driver.state["link"] == "online"
    assert set(kb.leds.values()) == {(0, 255, 0)}


def test_one_missed_reply_is_retried_not_taken_for_sleep(run):
    async def main():
        driver, kb, _, _ = await online()
        kb.drop_next = 1  # idle for a while: the first request goes unanswered
        await driver.invoke("set_lighting_color", {"value": "#00ff00"})
        return driver, kb

    driver, kb = run(main())
    assert driver.state["link"] == "online"
    assert set(kb.leds.values()) == {(0, 255, 0)}


def test_one_missed_battery_read_is_retried_not_taken_for_sleep(run, monkeypatch):
    from borochid_logitech_keyboard import driver as driver_module

    async def main():
        monkeypatch.setattr(driver_module, "BATTERY_POLL_S", 0.05)
        driver, kb, _, _ = await online()
        kb.drop_next = 1  # idle for minutes: the poll's first request goes unanswered
        kb.millivolts = 3700
        await asyncio.sleep(0.3)
        return driver

    driver = run(main())
    assert driver.state["link"] == "online" and driver.state["battery"] < 55


def test_quiet_with_the_link_up_is_looked_at_again_until_back(run, monkeypatch):
    from borochid_logitech_keyboard import driver as driver_module

    async def main():
        monkeypatch.setattr(driver_module, "ASLEEP_PROBE_S", 0.05)
        monkeypatch.setattr(driver_module, "BATTERY_POLL_S", 0.05)
        driver, kb, _, _ = await online()
        kb.asleep = True  # no reply to the battery poll, no link report either
        await asyncio.sleep(0.3)
        assert driver.state["link"] == "asleep"
        kb.asleep = False  # typed on: nothing the driver sees, but it answers again
        await asyncio.sleep(0.3)
        return driver, kb

    driver, kb = run(main())
    assert driver.state["link"] == "online" and kb.mode == 2 and kb.diverted


def test_a_lost_link_is_not_probed(run, monkeypatch):
    from borochid_logitech_keyboard import driver as driver_module

    async def main():
        monkeypatch.setattr(driver_module, "ASLEEP_PROBE_S", 0.05)
        driver, kb, _, _ = await online()
        kb.link(False)  # switched off: the receiver says when it's back
        await settle(driver)
        calls = len(kb.calls)
        await asyncio.sleep(0.3)
        return driver, kb, calls

    driver, kb, calls = run(main())
    assert driver.state["link"] == "asleep" and len(kb.calls) == calls


def test_power_cycle_takes_over_again(run):
    async def main():
        driver, kb, _, _ = await online()
        kb.power_cycle()
        await settle(driver)
        return kb

    kb = run(main())
    assert kb.mode == 2 and kb.diverted and kb.leds


def test_link_loss_releases_held_keys(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_binding", {"button": "g1", "binding": COPY})
        kb.press_g(1)
        await settle(driver, 1)
        kb.link(False)
        await settle(driver, 1)
        return driver

    driver = run(main())
    assert driver.state["link"] == "asleep"
    assert driver.host.input.events == [("down", "g1", COPY), ("up", "g1")]


def test_waits_for_the_broker(run):
    async def main():
        driver, kb, _, _ = make_driver()
        kb.up = False
        kb.problem = "broker down"
        await driver.start()
        await settle(driver)
        waiting = (driver.state["link"], kb.mode)
        kb.broker(True)
        await settle(driver)
        return driver, waiting

    driver, waiting = run(main())
    assert waiting == ("connecting", 1)
    assert driver.state["link"] == "online"


def test_stop_hands_the_keyboard_back(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_game_mode_keys", {"keys": [57]})
        await driver.stop()
        return kb

    kb = run(main())
    assert kb.mode == 1 and not kb.diverted and kb.sw_control == (0, 0) and kb.m_leds == 0 and kb.disabled == set()


def test_bad_stored_settings_fall_back_to_defaults(run):
    stored = {
        "profiles": {
            "default": {
                "report_rate": 7,
                "brightness": 55,
                "game_mode_keys": [227],
                "subprofile": 9,
                "subprofiles": [{"bindings": {"g1": {"keys": ["KEY_POWER"]}, "g2": COPY}, "lighting": {"mode": "disco", "keys": {"38": "red", "1": "#010203"}}}],
            }
        }
    }
    driver, _, _, _ = run(online(stored))
    s = driver.state
    assert s["report_rate"] == 1000 and s["brightness"] == 55 and s["game_mode_keys"] == [] and s["subprofile"] == 1
    assert s["bind.g1"] == "disabled" and s["bind.g2"] == COPY
    assert s["lighting.mode"] == "per_key" and s["lighting.keys"] == {"1": "#010203"}


def test_battery_charging_flags(run):
    async def main():
        driver, kb, _, _ = make_driver()
        kb.millivolts, kb.battery_flags = 4190, 0x80
        await driver.start()
        await settle(driver)
        return driver

    s = run(main()).state
    assert s["battery"] == 100 and s["charging"] is True


# -- X mode ------------------------------------------------------------------------


def test_x_mode_turns_m_keys_and_mr_into_macro_keys(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_x_mode", {"value": True})
        await driver.invoke("set_binding", {"button": "m2", "binding": COPY})
        await driver.invoke("set_binding", {"button": "mr", "binding": COPY})
        kb.press_m(2)
        kb.press_mr()
        await settle(driver)
        return driver, kb

    driver, kb = run(main())
    assert driver.state["subprofile"] == 1  # M2 didn't switch subprofile
    assert driver.host.input.events == [("down", "m2", COPY), ("up", "m2"), ("down", "mr", COPY), ("up", "mr")]
    assert kb.m_leds == 0b111 and kb.mr_led == 1
    assert driver.state["x_mode"] is True and driver.state["status"] == "Default · X mode"


def test_x_mode_uses_m1_and_off_goes_back_to_the_chosen_subprofile(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("select_subprofile", {"value": 2})
        await driver.invoke("set_lighting_color", {"value": "#00ff00"})  # M2's
        await driver.invoke("set_x_mode", {"value": True})
        in_x = set(kb.leds.values())
        await driver.invoke("set_x_mode", {"value": False})
        return driver, kb, in_x

    driver, kb, in_x = run(main())
    assert in_x == {BLUE}  # M1's lighting
    assert set(kb.leds.values()) == {(0, 255, 0)} and kb.m_leds == 0b010 and kb.mr_led == 0


def test_without_x_mode_mr_does_nothing(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_binding", {"button": "mr", "binding": COPY})
        kb.press_mr()
        await settle(driver)
        return driver

    assert run(main()).host.input.events == []


def test_x_mode_is_saved_with_the_profile(run):
    async def main():
        driver, _, _, store = await online()
        await driver.invoke("set_x_mode", {"value": True})
        again, _, _, _ = make_driver(store.data)
        return again

    assert run(main()).profiles.current.x_mode is True


# -- Num Lock status ---------------------------------------------------------------


def test_num_lock_key_is_dark_while_num_lock_is_off(run, tmp_path):
    led = tmp_path / "brightness"
    led.write_text("0\n")

    async def main():
        driver, kb, _, _ = make_driver()
        driver._numlock_path = led
        await driver.start()
        await settle(driver)
        await driver.invoke("set_numlock_light", {"value": True})
        off = kb.leds[80]
        led.write_text("1\n")
        await asyncio.sleep(NUMLOCK_POLL_S * 2)
        await settle(driver)
        return kb, off

    kb, off = run(main())
    assert off == (0, 0, 0) and kb.leds[80] == BLUE
    assert set(kb.leds.values()) == {BLUE}


def test_num_lock_status_off_leaves_the_key_lit(run, tmp_path):
    led = tmp_path / "brightness"
    led.write_text("0\n")

    async def main():
        driver, kb, _, _ = make_driver()
        driver._numlock_path = led
        await driver.start()
        await settle(driver)
        return kb

    assert run(main()).leds[80] == BLUE


def test_x_mode_lights_only_the_m_keys_asked_for(run):
    async def main():
        driver, kb, _, _ = await online()
        await driver.invoke("set_x_mode", {"value": True})
        await driver.invoke("set_x_light", {"key": "m2", "value": False})
        await driver.invoke("set_x_light", {"key": "mr", "value": False})
        return driver, kb

    driver, kb = run(main())
    assert kb.m_leds == 0b101 and kb.mr_led == 0
    assert driver.state["x_light.m2"] is False and driver.state["x_light.m1"] is True
    assert driver.profiles.current.x_lights == ["m1", "m3"]


def test_x_light_rejects_keys_without_a_white_led(run):
    async def main():
        driver, _, _, _ = await online()
        with pytest.raises(DriverError):
            await driver.invoke("set_x_light", {"key": "g1", "value": True})

    run(main())


def test_a_stored_game_mode_key_that_is_now_locked_is_dropped_not_the_list(run):
    stored = {"profiles": {"default": {"game_mode_keys": [4, 227]}}}  # 227 is locked
    driver, _, _, _ = make_driver(stored)
    assert driver.profiles.current.game_mode_keys == [4]


# -- one keyboard, one set of settings ----------------------------------------------


def test_the_keyboard_identifies_itself_by_unit_id(run):
    driver, _, _, _ = run(online())
    assert driver.device_id == "9454DCB7"


def test_receiver_and_cable_share_the_keyboards_settings(run, tmp_path):
    from borochid.common.models import Bus, DeviceIdentity
    from borochid.service.settings import SettingsStore

    receiver = SettingsStore(tmp_path, "logitech.g915", DeviceIdentity(Bus.USB, "usb:1-3"))
    cable = SettingsStore(tmp_path, "logitech.g915", DeviceIdentity(Bus.USB, "usb:1-4", serial="9454DCB7"))

    async def main():
        first, _, _, _ = make_driver(store=receiver)
        await first.use_profile(DEFAULT, {"default"})
        await first.start()
        await settle(first)
        await first.invoke("set_brightness", {"value": 40})  # through the receiver
        await first.stop()
        second, kb, _, _ = make_driver(store=cable)  # later, on the cable: a new file at first
        await second.use_profile(DEFAULT, {"default"})
        await second.start()
        await settle(second)
        return second, kb

    second, kb = run(main())
    assert second.profiles.current.brightness == 40 and kb.brightness == 40
    assert [p.name for p in (tmp_path / "device-settings/logitech.g915").iterdir()] == ["id-9454DCB7.driver.json"]


def test_without_report_rate_bluetooth_style_the_rest_still_works(run):
    async def main():
        driver, kb, _, _ = make_driver()
        kb.missing = {0x8060}  # over Bluetooth the link sets the rate
        kb.rate_ms = 8
        await driver.start()
        await settle(driver)
        await driver.invoke("set_report_rate", {"value": 500})  # kept for the other connections
        return driver, kb

    driver, kb = run(main())
    assert driver.state["link"] == "online" and driver.state["report_rate_available"] is False
    assert kb.rate_ms == 8 and driver.profiles.current.report_rate == 500
    assert set(kb.leds.values()) == {BLUE}


def test_num_lock_follows_the_led_when_it_moves(run, tmp_path):
    first, second = tmp_path / "input119", tmp_path / "input130"
    first.write_text("1\n")
    second.write_text("0\n")

    async def main():
        driver, kb, _, _ = make_driver()
        driver._numlock_path = first
        driver._find_numlock = lambda: second  # where the reconnected keyboard's LED is
        await driver.start()
        await settle(driver)
        await driver.invoke("set_numlock_light", {"value": True})
        lit = kb.leds[80]
        first.unlink()  # a Bluetooth reconnect: the old input device is gone
        await asyncio.sleep(NUMLOCK_POLL_S * 3)
        await settle(driver)
        return kb, lit

    kb, lit = run(main())
    assert lit == BLUE and kb.leds[80] == (0, 0, 0)  # found the new LED: Num Lock is off there


def test_num_lock_led_of_a_bluetooth_keyboard_is_found_by_its_address(tmp_path, monkeypatch):
    from borochid.common.models import Bus, DeviceIdentity

    from borochid_logitech_keyboard import driver as driver_module

    hid = tmp_path / "0005:046D:B354.0047"
    led = hid / "input" / "input130" / "input130::numlock" / "brightness"
    led.parent.mkdir(parents=True)
    led.write_text("0\n")
    (hid / "uevent").write_text("HID_ID=0005:0000046D:0000B354\nHID_NAME=G915 KEYBOARD\nHID_UNIQ=ef:24:28:5f:95:8a\n")
    monkeypatch.setattr(driver_module, "HID_SYSFS", tmp_path)
    driver, kb, _, _ = make_driver()
    kb.ident = DeviceIdentity(Bus.BLE, "ble:EF:24:28:5F:95:8A", attrs={"address": "EF:24:28:5F:95:8A"})
    assert driver._find_numlock() == led

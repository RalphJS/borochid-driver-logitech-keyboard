"""The real driver against the fake G915, with every report passing through
the broker's policy: whatever the driver needs must be allowed, and nothing
it sends may be refused."""

from __future__ import annotations

import asyncio

from conftest import LEDS, make_driver, settle

from borochid_logitech_keyboard.broker import policy
from borochid_logitech_keyboard.broker.policy import ANSWER, DEFER, Discovery, Policy


def police(kb):
    """Put the broker's policy between the driver and the fake keyboard, the
    way the broker's session does. Returns the policy and the list of
    requests it refused."""
    rules = Policy()
    refused: list[bytes] = []
    raw_write = kb.write
    to_driver = kb.on_data
    discovery: dict = {}
    deferred: list[bytes] = []

    def from_keyboard(report: bytes) -> None:
        d = discovery.get("round")
        if d is not None and d.matches(report):
            discovery["reply"].set_result(report)
            return
        if rules.check_incoming(report):
            to_driver(report)

    async def discover() -> None:
        d = Discovery(policy.DISCOVERY_SW[0])
        discovery["round"] = d
        while (r := d.request()) is not None:
            discovery["reply"] = asyncio.get_running_loop().create_future()
            await raw_write(r)
            assert d.feed(await asyncio.wait_for(discovery["reply"], 1))
        discovery.clear()
        rules.install(d.table)
        for r in deferred:
            to_driver(rules.answer_root(r))
        deferred.clear()

    async def write(data: bytes) -> None:
        action, out = rules.check_request(data)
        if action == ANSWER:
            if out[2] == policy.ERROR_20:
                refused.append(data)
            asyncio.get_running_loop().call_soon(to_driver, out)
        elif action == DEFER:
            deferred.append(data)
        else:
            await raw_write(rules.rewrite(data))

    kb.on_data = from_keyboard
    kb.write = write
    return rules, refused, discover


def test_the_driver_works_through_the_policy(run):
    async def main():
        driver, kb, _, _ = make_driver()
        rules, refused, discover = police(kb)
        await discover()
        await driver.start()
        await settle(driver)
        online = driver.state["link"]
        painted = dict(kb.leds)
        # Everything a session does: keys, subprofiles, every setting, every lighting mode.
        kb.press_g(0b1)
        kb.press_g(0)
        kb.press_m(2)
        kb.press_mr()
        kb.brightness_key()
        await settle(driver, 1)
        for action, params in [
            ("select_subprofile", {"value": 3}),
            ("set_report_rate", {"value": 500}),
            ("set_brightness", {"value": 60}),
            ("set_game_mode_keys", {"keys": [57]}),  # Caps Lock
            ("set_binding", {"button": "g2", "binding": {"keys": ["KEY_LEFTCTRL", "KEY_C"]}}),
            ("reset_binding", {"button": "g2"}),
            ("set_key_colors", {"keys": [1, 210], "color": "#ff0000"}),  # A and the logo
            ("clear_key_colors", {}),
            *(("set_lighting_mode", {"value": m}) for m in ("off", "cycle", "wave", "breathe", "ripple", "per_key")),
            ("set_lighting_color", {"value": "#00ff00"}),
            ("set_lighting_speed", {"value": 3}),
        ]:
            await driver.invoke(action, params)
            await settle(driver, 1)
        await driver.stop()
        return driver, kb, rules, refused, online, painted

    driver, kb, rules, refused, online, painted = run(main())
    assert refused == [], [r.hex(" ") for r in refused]
    assert online == "online"
    assert painted and set(painted) == set(LEDS)
    assert rules.index_of(0x1BC0) is None
    assert not any(f == 0x1BC0 for f, _, _ in kb.calls)
    assert kb.mode == 1 and not kb.diverted and kb.sw_control[0] == 0  # handed back on stop

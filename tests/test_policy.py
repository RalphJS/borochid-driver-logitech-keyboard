"""The broker's allowlist and feature discovery. Security-critical: every
rule has a test."""

from __future__ import annotations

import pytest

from borochid_logitech_keyboard.broker import policy
from borochid_logitech_keyboard.broker.policy import ALLOWED, ANSWER, DEFER, FORWARD, Discovery, Policy

SW = 0x0B
# Every feature of a G915 WIRELESS, at the index it reports.
G915 = {
    0x0001: 1, 0x0003: 2, 0x0005: 3, 0x1D4B: 4, 0x0020: 5, 0x0007: 6, 0x1001: 7, 0x1814: 8, 0x1815: 9,
    0x8071: 10, 0x8081: 11, 0x1B04: 12, 0x1BC0: 13, 0x4100: 14, 0x4522: 15, 0x4540: 16, 0x8010: 17,
    0x8020: 18, 0x8030: 19, 0x8040: 20, 0x8100: 21, 0x8060: 22, 0x00C2: 23, 0x00D0: 24, 0x1802: 25,
    0x1803: 26, 0x1806: 27, 0x1813: 28, 0x1805: 29, 0x1830: 30, 0x1890: 31, 0x1891: 32, 0x18A1: 33,
    0x1E00: 34, 0x1EB0: 35, 0x1861: 36, 0x18B0: 37,
}  # fmt: skip


def req(index: int, fn: int, *params: int, sw: int = SW) -> bytes:
    return bytes([0x11, 0xFF, index, (fn << 4) | sw, *params]).ljust(20, b"\0")


def reply(index: int, fn: int, *params: int, sw: int = SW) -> bytes:
    return bytes([0x11, 0x01, index, (fn << 4) | sw, *params]).ljust(20, b"\0")


def notification(index: int, fn: int, *params: int) -> bytes:
    return reply(index, fn, *params, sw=0)


def keyboard_reply(request: bytes, features: dict[int, int] = G915, device: int = 0x01) -> bytes:
    """What a keyboard with ``features`` answers to a discovery request."""
    at = {v: k for k, v in features.items()}
    index, fn_sw, p = request[2], request[3], request[4:]
    fn = fn_sw >> 4
    if index == 0 and fn == 0:
        out = [features.get((p[0] << 8) | p[1], 0), 0, 0]
    elif index == features[0x0001] and fn == 0:
        out = [max(at)]
    elif index == features[0x0001] and fn == 1:
        f = at.get(p[0], 0x18FF)  # a feature nothing here knows
        out = [f >> 8, f & 0xFF, 0x60 if f >> 8 == 0x18 else 0, 1]
    else:
        raise AssertionError(f"unexpected {request.hex(' ')}")
    return bytes([0x11, device, index, fn_sw, *out]).ljust(20, b"\0")


def discover(p: Policy, features: dict[int, int] = G915, sw: int = 0x0C) -> Discovery:
    d = Discovery(sw)
    while (r := d.request()) is not None:
        answer = keyboard_reply(r, features)
        assert d.matches(answer)
        assert d.feed(answer)
    assert d.done
    p.install(d.table)
    return d


def learned() -> Policy:
    p = Policy()
    discover(p)
    return p


def refused(p: Policy, report: bytes) -> bool:
    action, out = p.check_request(report)
    if action == FORWARD:
        return False
    assert action == ANSWER and out[:3] == bytes([0x11, 0xFF, 0xFF]), (action, out)
    assert out[3:5] == report[2:4].ljust(2, b"\0") and out[5] == policy.UNSUPPORTED and len(out) == 20
    return True


def root_answer(p: Policy, feature: int, sw: int = SW) -> bytes:
    action, out = p.check_request(req(0, 0, feature >> 8, feature & 0xFF, sw=sw))
    assert action == ANSWER
    return out


# -- discovery --------------------------------------------------------------------------


def test_discovery_finds_allowed_features_only():
    p = learned()
    assert p.index_to_id == {0: 0, **{i: f for f, i in G915.items() if f in ALLOWED}}
    for forbidden in (0x1BC0, 0x1B04, 0x00C2, 0x00D0, 0x1E00, 0x1EB0, 0x1814, 0x0020, 0x0001):
        assert p.index_of(forbidden) is None
    assert G915[0x1BC0] not in p.index_to_id


def test_discovery_walks_feature_set_serially():
    d = Discovery(0x0D)
    sent = []
    while (r := d.request()) is not None:
        sent.append((r[2], r[3], r[4]))
        d.feed(keyboard_reply(r))
    assert sent[0] == (0, 0x0D, 0x00) and sent[1] == (1, 0x0D, 0)
    assert [s[2] for s in sent[2:]] == list(range(1, 38))
    assert all(s[1] & 0x0F == 0x0D for s in sent)


def test_discovery_needs_a_reserved_software_id():
    for sw in (0, 1, SW, policy.RESTORE_SW):
        with pytest.raises(ValueError):
            Discovery(sw)


def test_discovery_ignores_replies_that_are_not_its_own():
    d = Discovery(0x0C)
    r = d.request()
    good = keyboard_reply(r)
    for other in (good[:3] + bytes([0x0B]) + good[4:],  # the client's sw
                  good[:3] + bytes([0x0D]) + good[4:],  # another round's
                  notification(0, 0, 1)):
        assert not d.matches(other)
        assert not d.feed(other)
    assert d.request() == r  # still waiting for the same reply


def test_discovery_aborts_on_error_reply():
    d = Discovery(0x0C)
    d.feed(keyboard_reply(d.request()))
    r = d.request()
    err = bytes([0x11, 0x01, 0xFF, r[2], r[3], 0x08]).ljust(20, b"\0")
    assert d.matches(err) and not d.feed(err) and not d.done


def test_discovery_aborts_without_feature_set():
    d = Discovery(0x0C)
    assert not d.feed(keyboard_reply(d.request(), {0x0001: 0}))


def test_discovery_rejects_a_feature_listed_twice():
    d = Discovery(0x0C)
    while (r := d.request()) is not None:
        answer = keyboard_reply(r)
        if r[2] == 1 and r[3] >> 4 == 1 and r[4] == G915[0x8010]:
            answer = answer[:4] + bytes([0x81, 0x00]) + answer[6:]  # claims ONBOARD_PROFILES too
        if not d.feed(answer):
            break
    assert not d.done


def test_only_a_completed_round_replaces_the_table():
    p = learned()
    before = dict(p.features)
    d = Discovery(0x0D)
    for _ in range(3):  # FEATURE_SET index, count, index 1; then abandoned
        d.feed(keyboard_reply(d.request()))
    assert p.features == before and not d.done


def test_install_drops_anything_not_allowed():
    p = Policy()
    p.install({13: (0x1BC0, 0, 0), 17: (0x8010, 0, 0), 0: (0x8040, 0, 0), 0xFF: (0x8040, 0, 0)})
    assert p.index_to_id == {0: 0, 17: 0x8010}


# -- the review's attack: late or spoofed replies can't map a feature to another's index --


def test_client_root_queries_never_reach_the_keyboard_or_teach_anything():
    p = Policy()
    assert p.check_request(req(0, 0, 0x81, 0x00)) == (DEFER, None)
    # A reply with the client's sw claiming ONBOARD_PROFILES sits at
    # PER_KEY_LIGHTING's index (late or spoofed): not a reply to anything.
    assert not p.check_incoming(reply(0, 0, G915[0x8081]))
    discover(p)
    assert p.index_of(0x8100) == G915[0x8100]
    assert not p.check_incoming(reply(0, 0, G915[0x8081]))
    assert p.index_of(0x8100) == G915[0x8100]


def test_late_replies_between_queries_change_nothing():
    p = learned()
    table = dict(p.features)
    for feature in (0x8081, 0x8100, 0x4522, 0x8071):
        root_answer(p, feature)
        for sw in (SW, *policy.RESERVED_SW):
            assert not p.check_incoming(reply(0, 0, G915[0x8100], sw=sw))
    assert p.features == table
    # So profile memory writes and NvConfig stay refused.
    for fn in (5, 6, 7, 8):
        assert refused(p, req(G915[0x8100], fn))
    assert refused(p, req(G915[0x8071], 3))


def test_stale_reply_from_an_aborted_round_cannot_match_the_next():
    first = Discovery(0x0C).request()  # sent, then the round timed out
    d2 = Discovery(0x0D)
    stale = keyboard_reply(first, {0x0001: G915[0x8100]})  # arrives now, wrong answer
    assert not d2.matches(stale) and not d2.feed(stale)


# -- ROOT answered from the table ---------------------------------------------------------


def test_root_answered_from_the_table_with_the_clients_software_id():
    p = learned()
    for feature in ALLOWED:
        if feature == policy.ROOT:
            continue
        out = root_answer(p, feature, sw=0x0A)
        assert out[:4] == bytes([0x11, 0x01, 0x00, 0x0A]) and out[4] == G915[feature] and len(out) == 20


def test_forbidden_or_missing_features_read_as_unsupported():
    p = learned()
    for feature in (0x1BC0, 0x1B04, 0x00C2, 0x00D0, 0x1E00, 0x1EB0, 0x1814, 0x0020, 0x0001, 0x0000, 0xFFFF):
        assert root_answer(p, feature)[4:7] == b"\0\0\0", hex(feature)
    p2 = Policy()
    discover(p2, {f: i for f, i in G915.items() if f != 0x8030})
    assert root_answer(p2, 0x8030)[4:7] == b"\0\0\0"


def test_root_query_before_discovery_is_deferred():
    p = Policy()
    assert p.check_request(req(0, 0, 0x80, 0x40)) == (DEFER, None)
    discover(p)
    assert p.answer_root(req(0, 0, 0x80, 0x40))[4] == G915[0x8040]


def test_ping_is_forwarded():
    assert Policy().check_request(req(0, 1, 0, 0, 0x5A)) == (FORWARD, None)


def test_other_root_functions_refused():
    p = learned()
    for fn in range(2, 16):
        assert refused(p, req(0, fn))


# -- request rules ---------------------------------------------------------------------


def test_nothing_but_root_is_reachable_before_discovery():
    p = Policy()
    for index in range(1, 256):
        assert refused(p, req(index, 0))


def test_guessing_the_keylogging_feature_index_gets_nothing():
    p = learned()
    for fn in range(16):
        assert refused(p, req(G915[0x1BC0], fn, 1))


def test_every_forbidden_feature_is_unreachable():
    p = learned()
    for feature, index in G915.items():
        if feature not in ALLOWED:
            for fn in range(16):
                assert refused(p, req(index, fn)), (hex(feature), fn)


def test_only_long_reports_of_20_bytes():
    p = learned()
    assert refused(p, bytes([0x10, 0xFF, 0, 0x1B, 0, 0, 0x5A]))
    assert refused(p, req(0, 1)[:19])
    assert refused(p, req(0, 1) + b"\0")
    assert refused(p, bytes([0x12]) + req(0, 1)[1:])
    assert refused(p, b"\x11")


def test_software_id_zero_and_reserved_ids_refused():
    p = learned()
    for sw in (0, *policy.RESERVED_SW):
        assert refused(p, req(0, 1, sw=sw))
        assert refused(p, req(G915[0x8040], 1, sw=sw))
        assert refused(p, req(0, 0, 0x80, 0x40, sw=sw))


@pytest.mark.parametrize(
    ("feature", "allowed", "denied"),
    [
        (0x0003, [0, 1], [2, 3, 15]),
        (0x0005, [0, 1, 2], [3, 15]),
        (0x1001, [0], [1, 2]),
        (0x1D4B, [], [0, 1]),
        (0x4522, [0, 1, 2, 3], [4, 15]),
        (0x4540, [0], [1, 2]),
        (0x8020, [0, 1], [2, 3]),
        (0x8040, [0, 1, 2], [3, 4]),
        (0x8060, [0, 1, 2], [3]),
        (0x8081, [0, 1, 5, 6, 7], [2, 3, 4, 8, 9]),
        (0x8100, [0, 2], [3, 4, 5, 6, 7, 8, 0x0B, 0x0C]),
        (0x8071, [0], [2, 3, 4, 6, 7, 8, 9]),
    ],
)
def test_functions(feature, allowed, denied):
    p = learned()
    index = G915[feature]
    for fn in allowed:
        assert not refused(p, req(index, fn)), fn
    for fn in denied:
        assert refused(p, req(index, fn)), fn


def test_gkey_divert_only_on_or_off():
    p = learned()
    i = G915[0x8010]
    assert not refused(p, req(i, 0))
    assert not refused(p, req(i, 2, 0)) and not refused(p, req(i, 2, 1))
    assert refused(p, req(i, 2, 2))
    assert refused(p, req(i, 1)) and refused(p, req(i, 3))


def test_mr_led_only_on_or_off():
    p = learned()
    i = G915[0x8030]
    assert not refused(p, req(i, 0, 0)) and not refused(p, req(i, 0, 1))
    assert refused(p, req(i, 0, 2)) and refused(p, req(i, 1))


def test_effect_must_not_persist():
    p = learned()
    i = G915[0x8071]
    ram = [1, 1, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x01]
    assert not refused(p, req(i, 1, *ram))
    assert refused(p, req(i, 1, *ram[:12], 0x00))
    assert refused(p, req(i, 1, *ram[:12], 0x02))


def test_sw_control_forms():
    p = learned()
    i = G915[0x8071]
    assert not refused(p, req(i, 5, 0))
    assert not refused(p, req(i, 5, 1, 3, 4))
    assert not refused(p, req(i, 5, 1, 0, 0))
    for bad in [(1, 3, 5), (1, 3, 7), (1, 1, 0), (2, 0, 0)]:
        assert refused(p, req(i, 5, *bad)), bad


def test_frame_end_arguments_must_be_zero():
    p = learned()
    i = G915[0x8081]
    assert not refused(p, req(i, 7))
    for pos in range(5):
        params = [0] * 5
        params[pos] = 1
        assert refused(p, req(i, 7, *params))


def test_onboard_mode_only_onboard_or_host():
    p = learned()
    i = G915[0x8100]
    assert not refused(p, req(i, 1, 1)) and not refused(p, req(i, 1, 2))
    assert refused(p, req(i, 1, 0)) and refused(p, req(i, 1, 3))


def test_rewrite_always_addresses_the_keyboard():
    for dev in (0x00, 0x02, 0xFF):
        r = bytes([0x11, dev]) + req(0, 1)[2:]
        assert Policy().rewrite(r)[1] == 1  # behind a receiver: its slot
        assert Policy(policy.WIRED).rewrite(r)[1] == 0xFF  # on a cable: the device itself
        assert Policy().rewrite(r)[2:] == r[2:]


def test_on_a_cable_only_the_keyboards_own_reports_come_back():
    p = Policy(policy.WIRED)
    p.install({G915[0x8010]: (0x8010, 0, 0)})
    assert p.check_incoming(bytes([0x11, 0xFF, G915[0x8010], 0x00, 1]).ljust(20, b"\0"))  # G1, from the keyboard
    assert not p.check_incoming(bytes([0x11, 0x01, G915[0x8010], 0x00, 1]).ljust(20, b"\0"))
    assert not p.check_incoming(bytes([0x10, 0xFF, 0x41, 0x04, 0x61, 0x3E, 0xC3]))  # no receiver link reports


# -- incoming ----------------------------------------------------------------------------


def test_reply_forwarded_once_and_only_when_pending():
    p = learned()
    i = G915[0x8040]
    assert not p.check_incoming(reply(i, 1, 0, 100))
    p.check_request(req(i, 1))
    assert p.check_incoming(reply(i, 1, 0, 100))
    assert not p.check_incoming(reply(i, 1, 0, 100))


def test_error_replies_forwarded_when_pending():
    p = learned()
    i = G915[0x8040]
    p.check_request(req(i, 2, 0, 50))
    assert p.check_incoming(bytes([0x11, 0x01, 0xFF, i, 0x2B, 0x02]).ljust(20, b"\0"))
    p.check_request(req(i, 1))
    assert p.check_incoming(bytes([0x10, 0x01, 0x8F, i, 0x1B, 0x04, 0]))
    assert not p.check_incoming(bytes([0x11, 0x01, 0xFF, i, 0x1B, 0x02]).ljust(20, b"\0"))


def test_broker_replies_never_forwarded():
    p = learned()
    for sw in policy.RESERVED_SW:
        assert not p.check_incoming(reply(0, 0, 1, sw=sw))
        assert not p.check_incoming(reply(G915[0x8010], 2, sw=sw))


def test_notifications_only_from_notify_features():
    p = learned()
    for feature in policy.NOTIFY:
        assert p.check_incoming(notification(G915[feature], 0, 1)), hex(feature)
    for feature in (0x8071, 0x8081, 0x8100, 0x4522, 0x0005):
        assert not p.check_incoming(notification(G915[feature], 0, 1)), hex(feature)
    assert not p.check_incoming(notification(G915[0x1BC0], 0, 0, 0x04))  # a key report, were it ever on
    assert not p.check_incoming(notification(99, 0, 1))


def test_notifications_before_discovery_dropped():
    assert not Policy().check_incoming(notification(17, 0, 1))


def test_receiver_link_notifications_forwarded():
    p = Policy()
    assert p.check_incoming(bytes([0x10, 0x01, 0x41, 0x14, 0x7C, 0x40, 0]))
    assert p.check_incoming(bytes([0x10, 0x01, 0x40, 0x02, 0, 0, 0]))
    assert not p.check_incoming(bytes([0x10, 0x02, 0x41, 0x14, 0x7C, 0x40, 0]))


def test_receiver_register_replies_and_other_devices_dropped():
    p = learned()
    assert not p.check_incoming(bytes([0x10, 0xFF, 0x81, 0x00, 0, 0x09, 0]))
    assert not p.check_incoming(bytes([0x10, 0xFF, 0x8F, 0x81, 0x00, 0x02, 0]))
    p.check_request(req(G915[0x8040], 1))
    assert not p.check_incoming(bytes([0x11, 0x02, G915[0x8040], 0x1B, 0, 100]).ljust(20, b"\0"))
    assert not p.check_incoming(bytes([0x12, 0x01]) + bytes(62))
    assert not p.check_incoming(b"\x11\x01")


def test_pending_is_bounded():
    p = learned()
    i = G915[0x8040]
    for _ in range(policy.PENDING_MAX + 10):
        p.check_request(req(i, 1))
    assert len(p._pending) == policy.PENDING_MAX


# -- restore ------------------------------------------------------------------------------


def test_restore_only_discovered_and_onboard_last():
    assert Policy().restore_requests() == []
    p = Policy()
    discover(p, {0x0001: 1, 0x8100: 2, 0x8010: 3})
    assert p.restore_requests() == [(0x8010, 2, (0,)), (0x8100, 1, (1,))]
    full = learned().restore_requests()
    assert [r[0] for r in full] == [0x8010, 0x8030, 0x8020, 0x8071, 0x4522, 0x8100]


def test_restore_requests_pass_the_policy_themselves():
    p = learned()
    for feature, fn, params in p.restore_requests():
        assert p.check_request(policy.build_request(p.index_of(feature), fn, SW, params)) == (FORWARD, None)

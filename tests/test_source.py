import json

import pytest
from octodns.zone import Zone

from octodns_technitium import TechnitiumError, TechnitiumSource


def rec(name, type, rdata, ttl=300, **kw):
    return {"name": name, "type": type, "ttl": ttl, "disabled": False, "rData": rdata, **kw}


def app(name, views, ttl=300, **kw):
    rdata = {"classPath": "SplitHorizon.SimpleAddress", "data": json.dumps(views)}
    return rec(name, "APP", rdata, ttl, **kw)


SOA = rec("example.com", "SOA", {"serial": 1})


def populate(fake, source, *records):
    fake.records["example.com"] = [SOA, *records]
    zone = Zone("example.com.", [])
    source.populate(zone)
    return {(r.name, r._type): r for r in zone.records}


def test_plain_records(fake, source):
    got = populate(
        fake,
        source,
        rec("example.com", "A", {"ipAddress": "1.1.1.1"}, 600),
        rec("example.com", "A", {"ipAddress": "1.1.1.1"}, 600),
        rec("example.com", "A", {"ipAddress": "8.8.8.8"}, 100),
        rec("www.example.com", "CNAME", {"cname": "Example.com"}),
        rec("example.com", "MX", {"preference": 10, "exchange": "mail.example.com"}),
        rec(
            "_sip._tcp.example.com",
            "SRV",
            {"priority": 1, "weight": 2, "port": 5060, "target": "sip.example.com"},
        ),
        rec("example.com", "CAA", {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}),
        rec("sub.example.com", "NS", {"nameServer": "ns.example.net"}),
        rec("v6.example.com", "AAAA", {"ipAddress": "2606:4700::1111"}),
    )
    a = got[("", "A")]
    assert sorted(a.values) == ["1.1.1.1", "8.8.8.8"]
    assert a.ttl == 100
    assert got[("www", "CNAME")].value == "example.com."
    assert got[("", "MX")].values[0].exchange == "mail.example.com."
    assert got[("_sip._tcp", "SRV")].values[0].port == 5060
    assert got[("", "CAA")].values[0].value == "letsencrypt.org"
    assert got[("sub", "NS")].values == ["ns.example.net."]
    assert got[("v6", "AAAA")].values == ["2606:4700::1111"]
    assert fake.requests[0][1] == {
        "domain": "example.com",
        "zone": "example.com",
        "listZone": "true",
    }


def test_app_public_split(fake, source):
    got = populate(
        fake,
        source,
        app(
            "home.example.com",
            {"public": ["1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8"], "internal": ["10.0.0.1"]},
        ),
    )
    assert sorted(got[("home", "A")].values) == ["1.1.1.1", "8.8.8.8"]
    assert got[("home", "AAAA")].values == ["2606:4700::1111"]


def test_other_app_ignored(fake, source):
    r = app("x.example.com", {"public": ["1.1.1.1"]})
    r["rData"]["classPath"] = "Other.App"
    assert ("x", "A") not in populate(fake, source, r)


def test_private_addresses_dropped_and_warned_once(fake, source, caplog):
    records = [
        rec("a.example.com", "A", {"ipAddress": ip})
        for ip in ("10.0.0.1", "100.64.1.1", "127.0.0.1", "169.254.1.1", "192.168.1.1", "1.1.1.1")
    ] + [rec("a.example.com", "AAAA", {"ipAddress": ip}) for ip in ("fd00::1", "fe80::1", "::1")]
    got = populate(fake, source, *records)
    assert got[("a", "A")].values == ["1.1.1.1"]
    assert ("a", "AAAA") not in got
    assert len([m for m in caplog.messages if "not a public address" in m]) == 8
    caplog.clear()
    populate(fake, source, *records)
    assert not [m for m in caplog.messages if "not a public address" in m]


def test_public_only_off_keeps_private(fake):
    src = TechnitiumSource("t", fake.url, "secret", public_only=False)
    assert populate(fake, src, rec("a.example.com", "A", {"ipAddress": "10.0.0.1"}))[
        ("a", "A")
    ].values == ["10.0.0.1"]


def test_disabled_and_apex_ns_skipped(fake, source):
    got = populate(
        fake,
        source,
        rec("off.example.com", "A", {"ipAddress": "1.1.1.1"}, disabled=True),
        app("off2.example.com", {"public": ["1.1.1.1"]}, disabled=True),
        rec("example.com", "NS", {"nameServer": "ns.example.net"}),
    )
    assert got == {}


def test_view_option(fake):
    src = TechnitiumSource("t", fake.url, "secret", view="other")
    got = populate(fake, src, app("a.example.com", {"public": ["1.1.1.1"], "other": ["8.8.8.8"]}))
    assert got[("a", "A")].values == ["8.8.8.8"]


def test_proxied(fake, source):
    got = populate(
        fake,
        source,
        rec("a.example.com", "A", {"ipAddress": "1.1.1.1"}, comments="web cf:proxied"),
        rec("b.example.com", "A", {"ipAddress": "1.1.1.1"}, comments="plain"),
        rec("c.example.com", "TXT", {"text": "x"}, comments="cf:proxied"),
        app("d.example.com", {"public": ["1.1.1.1"]}, comments="cf:proxied"),
    )
    assert got[("a", "A")].octodns["cloudflare"] == {"proxied": True}
    assert "cloudflare" not in got[("b", "A")].octodns
    assert "cloudflare" not in got[("c", "TXT")].octodns
    assert got[("d", "A")].octodns["cloudflare"] == {"proxied": True}


def test_txt_escaping(fake, source):
    got = populate(fake, source, rec("example.com", "TXT", {"text": "v=DKIM1; k=rsa; p=abc"}))
    assert got[("", "TXT")].values == ["v=DKIM1\\; k=rsa\\; p=abc"]


def test_bad_app_json_raises(fake, source):
    r = app("a.example.com", {})
    r["rData"]["data"] = "{not json"
    with pytest.raises(TechnitiumError, match="a.example.com"):
        populate(fake, source, r)


def test_api_errors_raise(fake, source):
    with pytest.raises(TechnitiumError, match="No such zone"):
        source.populate(Zone("other.com.", []))
    with pytest.raises(TechnitiumError, match="invalid-token"):
        TechnitiumSource("t", fake.url, "wrong").populate(Zone("example.com.", []))


def test_empty_zone_raises(fake, source):
    fake.records["example.com"] = []
    with pytest.raises(TechnitiumError, match="no records"):
        source.populate(Zone("example.com.", []))

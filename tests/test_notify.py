import os
import stat
import threading
import time

import dns.flags
import dns.message
import dns.opcode
import dns.query
import dns.rcode
import pytest

import octodns_notify as n


def wait_for(cond, timeout=5):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


class Fake:
    """A sync that records its calls; results are popped per zone, default success."""

    def __init__(self):
        self.calls = []
        self.results = {}
        self.gate = None  # set to an Event to block syncs until it is set

    def __call__(self, zone):
        self.calls.append(zone)
        if self.gate:
            self.gate.wait(5)
        return self.results.get(zone, [True]).pop(0) if self.results.get(zone) else True


@pytest.fixture
def start():
    started = []

    def start(zones, fake, backoff=0.05):
        syncer = n.Syncer(zones.values(), fake, backoff=backoff)
        server = n.NotifyServer("127.0.0.1", 0, zones, syncer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        worker = threading.Thread(target=syncer.run)
        worker.start()
        started.append((server, syncer, worker))
        return server.server_address[1]

    yield start
    for server, syncer, worker in started:
        server.shutdown()
        server.server_close()
        syncer.stop()
        worker.join()


def send(port, zone, opcode=dns.opcode.NOTIFY):
    q = dns.message.make_query(zone, "SOA")
    q.set_opcode(opcode)
    return dns.query.udp(q, "127.0.0.1", port=port, timeout=2)


def test_load_zones(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("zones:\n  Example.com.: {}\n  '*.arpa.': {}\n  b.org.: {}\n")
    assert n.load_zones(str(f)) == {"example.com.": "Example.com.", "b.org.": "b.org."}


def test_startup_syncs_all_zones(start):
    fake = Fake()
    start({"a.org.": "a.org.", "b.org.": "b.org."}, fake)
    wait_for(lambda: sorted(fake.calls) == ["a.org.", "b.org."])


def test_notify_answered_and_synced(start):
    fake = Fake()
    port = start({"a.org.": "a.org."}, fake)
    wait_for(lambda: fake.calls == ["a.org."])
    r = send(port, "A.org.")
    assert r.rcode() == dns.rcode.NOERROR
    assert r.opcode() == dns.opcode.NOTIFY
    assert r.flags & dns.flags.AA
    wait_for(lambda: len(fake.calls) == 2)


def test_unknown_zone_and_other_opcodes_refused(start):
    fake = Fake()
    port = start({"a.org.": "a.org."}, fake)
    wait_for(lambda: fake.calls == ["a.org."])
    assert send(port, "other.org.").rcode() == dns.rcode.REFUSED
    assert send(port, "a.org.", dns.opcode.QUERY).rcode() == dns.rcode.REFUSED
    time.sleep(0.2)
    assert fake.calls == ["a.org."]


def test_duplicates_collapse(start):
    fake = Fake()
    fake.gate = threading.Event()
    port = start({"a.org.": "a.org."}, fake)
    wait_for(lambda: fake.calls == ["a.org."])  # startup sync is running
    for _ in range(5):
        assert send(port, "a.org.").rcode() == dns.rcode.NOERROR
    fake.gate.set()
    wait_for(lambda: len(fake.calls) == 2)
    time.sleep(0.3)
    assert len(fake.calls) == 2


def test_failure_is_retried(start):
    fake = Fake()
    fake.results["a.org."] = [False, False]
    start({"a.org.": "a.org."}, fake)
    wait_for(lambda: len(fake.calls) == 3)
    time.sleep(0.3)
    assert len(fake.calls) == 3


def test_resync_runs_all_zones():
    fake = Fake()
    s = n.Syncer(["a.org."], fake, resync=1)
    t = threading.Thread(target=s.run)
    t.start()
    try:
        wait_for(lambda: len(fake.calls) == 2, timeout=4)
    finally:
        s.stop()
        t.join()


def test_octodns_sync_command(tmp_path, monkeypatch):
    out = tmp_path / "args"
    script = tmp_path / "octodns-sync"
    script.write_text(f'#!/bin/sh\necho "$@" > {out}\n[ "$3" != "--doit" ] || exit 1\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert n.octodns_sync("c.yaml", False)("a.org.") is True
    assert out.read_text().strip() == "--config-file c.yaml a.org."
    assert n.octodns_sync("c.yaml", True)("a.org.") is False
    assert out.read_text().strip() == "--config-file c.yaml --doit a.org."

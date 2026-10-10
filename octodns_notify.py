"""Runs octodns-sync for a zone when a DNS NOTIFY for it arrives."""

import argparse
import logging
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

import dns.flags
import dns.message
import dns.opcode
import dns.rcode
import yaml

BACKOFF = 30.0

log = logging.getLogger("octodns-notify")


def load_zones(config_file):
    """Maps lowercase zone names to the names used in the config, wildcards skipped."""
    with open(config_file) as f:
        config = yaml.safe_load(f)
    return {name.lower(): name for name in config.get("zones") or {} if "*" not in name}


def octodns_sync(config_file, doit):
    def sync(zone):
        cmd = ["octodns-sync", "--config-file", config_file]
        if doit:
            cmd.append("--doit")
        cmd.append(zone)
        # octodns-sync logs to stderr; keep everything on stdout.
        return subprocess.run(cmd, stderr=subprocess.STDOUT).returncode == 0

    return sync


class Syncer:
    """Runs sync(zone) -> bool one zone at a time; a zone is queued at most once."""

    def __init__(self, zones, sync, resync=0, backoff=BACKOFF):
        self.zones = list(zones)
        self.sync = sync
        self.resync = resync
        self.backoff = backoff
        self._due = {}  # zone -> monotonic time it may run
        self._cond = threading.Condition()
        self._stopped = False

    def queue(self, zone, delay=0.0):
        with self._cond:
            at = time.monotonic() + delay
            self._due[zone] = min(self._due.get(zone, at), at)
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def _next(self, next_resync):
        with self._cond:
            while not self._stopped:
                now = time.monotonic()
                if self.resync and now >= next_resync[0]:
                    next_resync[0] += self.resync
                    for zone in self.zones:
                        self.queue(zone)
                ready = [zone for zone, at in self._due.items() if at <= now]
                if ready:
                    zone = min(ready, key=self._due.get)
                    del self._due[zone]
                    return zone
                wake = list(self._due.values()) + ([next_resync[0]] if self.resync else [])
                self._cond.wait(min(wake) - now if wake else None)

    def run(self):
        for zone in self.zones:
            self.queue(zone)
        next_resync = [time.monotonic() + self.resync]
        while zone := self._next(next_resync):
            log.info("syncing %s", zone)
            try:
                ok = self.sync(zone)
            except Exception:
                log.exception("sync of %s crashed", zone)
                ok = False
            if ok:
                log.info("synced %s", zone)
            else:
                log.warning("sync of %s failed, retrying in %gs", zone, self.backoff)
                self.queue(zone, self.backoff)


class NotifyServer(socketserver.UDPServer):
    def __init__(self, host, port, zones, syncer):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.zones = zones
        self.syncer = syncer
        super().__init__((host, port), NotifyHandler)

    def answer(self, data, peer):
        try:
            query = dns.message.from_wire(data)
        except Exception:
            return None
        if query.flags & dns.flags.QR:
            return None  # never answer a response
        reply = dns.message.make_response(query)
        reply.set_rcode(dns.rcode.REFUSED)
        if query.opcode() == dns.opcode.NOTIFY and query.question:
            zone = self.zones.get(query.question[0].name.to_text().lower())
            if zone:
                log.info("NOTIFY for %s from %s", zone, peer[0])
                reply.flags |= dns.flags.AA
                reply.set_rcode(dns.rcode.NOERROR)
                self.syncer.queue(zone)
            else:
                log.warning("NOTIFY for unknown zone %s from %s", query.question[0].name, peer[0])
        return reply.to_wire()


class NotifyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        reply = self.server.answer(data, self.client_address)
        if reply:
            sock.sendto(reply, self.client_address)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config-file", required=True, help="octoDNS config file")
    p.add_argument("--doit", action="store_true", help="apply the plan, default is a dry run")
    p.add_argument("--listen", default="0.0.0.0", help="address of the NOTIFY listener")
    p.add_argument("--port", type=int, default=5353, help="UDP port of the NOTIFY listener")
    p.add_argument(
        "--resync", type=int, default=600, help="seconds between syncs of all zones, 0 disables"
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    zones = load_zones(args.config_file)
    if not zones:
        p.error("no zones in the config file")

    syncer = Syncer(zones.values(), octodns_sync(args.config_file, args.doit), args.resync)
    server = NotifyServer(args.listen, args.port, zones, syncer)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    worker = threading.Thread(target=syncer.run)
    worker.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("listening on %s:%d for %d zones", args.listen, args.port, len(zones))
    stop.wait()

    log.info("stopping")
    server.shutdown()
    server.server_close()
    syncer.stop()
    worker.join()  # a running sync finishes first


if __name__ == "__main__":
    main()

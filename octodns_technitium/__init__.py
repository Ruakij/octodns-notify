import ipaddress
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from octodns.record import Record
from octodns.source.base import BaseSource

__version__ = "0.1.0"

PROXIED_MARKER = "cf:proxied"

# Shared address space is neither private nor global to ipaddress.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_public(addr):
    """Global unicast, not private (RFC 1918, ULA), shared (CGNAT), loopback or link-local."""
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or (addr.version == 4 and addr in _CGNAT)
    )


def _fqdn(name):
    # A bare "." is the null target of RFC 7505 and RFC 6186.
    name = name.lower().rstrip(".")
    return name + "." if name else "."


class TechnitiumError(Exception):
    pass


class TechnitiumSource(BaseSource):
    """
    Reads the records of Technitium DNS zones over its HTTP API.

    sources:
      technitium:
        class: octodns_technitium.TechnitiumSource
        url: https://dns.example.com
        token: env/TECHNITIUM_TOKEN
        # Split Horizon view of SimpleAddress APP records (default: public)
        view: public
        # Leave out addresses that are not public (default: true)
        public_only: true
        # Request timeout in seconds (default: 30)
        timeout: 30
    """

    SUPPORTS_GEO = False
    SUPPORTS = {"A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS"}

    def __init__(self, id, url, token, view="public", timeout=30, public_only=True):
        self.log = logging.getLogger(f"TechnitiumSource[{id}]")
        super().__init__(id)
        self.url = url.rstrip("/")
        self.token = token
        self.view = view
        self.timeout = timeout
        self.public_only = public_only
        self._warned = set()

    def _call(self, path, **params):
        req = urllib.request.Request(
            f"{self.url}{path}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()  # Technitium explains errors in the body
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise TechnitiumError(f"{path}: not a JSON response: {e}") from e
        if out.get("status") != "ok":
            raise TechnitiumError(f"{path}: {out.get('status')}: {out.get('errorMessage')}")
        return out["response"]

    def _addresses(self, name, rec):
        """Addresses of a SimpleAddress APP record in the configured view."""
        try:
            views = json.loads(rec["rData"].get("data") or "{}")
            return [ipaddress.ip_address(a) for a in views.get(self.view, [])]
        except (ValueError, AttributeError, TypeError) as e:
            # Skipping the record would delete the addresses of the name downstream.
            raise TechnitiumError(f"APP record {name}: unreadable data: {e}") from e

    def _values(self, zone_name, name, rec):
        """Yields (type, value) of one Technitium record."""
        t, d = rec["type"], rec["rData"]
        if t in ("A", "AAAA"):
            yield t, ipaddress.ip_address(d["ipAddress"])
        elif t == "CNAME":
            yield t, _fqdn(d["cname"])
        elif t == "NS":
            if name != zone_name:  # the target owns the apex NS
                yield t, _fqdn(d["nameServer"])
        elif t == "MX":
            yield t, {"preference": d["preference"], "exchange": _fqdn(d["exchange"])}
        elif t == "TXT":
            yield t, d["text"].replace(";", "\\;")
        elif t == "SRV":
            yield (
                t,
                {
                    "priority": d["priority"],
                    "weight": d["weight"],
                    "port": d["port"],
                    "target": _fqdn(d["target"]),
                },
            )
        elif t == "CAA":
            yield t, {"flags": d["flags"], "tag": d["tag"].lower(), "value": d["value"]}
        elif t == "APP" and d.get("classPath", "").endswith(".SimpleAddress"):
            for a in self._addresses(name, rec):
                a = getattr(a, "ipv4_mapped", None) or a
                yield ("A" if a.version == 4 else "AAAA"), a

    def populate(self, zone, target=False, lenient=False):
        zone_name = zone.name.rstrip(".").lower()
        self.log.debug("populate: name=%s, target=%s, lenient=%s", zone.name, target, lenient)
        recs = self._call(
            "/api/zones/records/get", domain=zone_name, zone=zone_name, listZone="true"
        )["records"]
        # A zone always has an SOA; an empty list would delete everything downstream.
        if not recs:
            raise TechnitiumError(f"zone {zone_name} returned no records")

        groups = {}
        for rec in recs:
            if rec.get("disabled"):
                continue
            name = rec["name"].lower()
            for typ, value in self._values(zone_name, name, rec):
                if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                    if self.public_only and not is_public(value):
                        if value not in self._warned:
                            self._warned.add(value)
                            self.log.warning(
                                "%s %s %s is not a public address, left out", name, typ, value
                            )
                        continue
                    value = str(value)
                rel = "" if name == zone_name else name.removesuffix("." + zone_name)
                g = groups.setdefault(
                    (rel, typ), {"ttl": rec["ttl"], "values": [], "proxied": False}
                )
                g["ttl"] = min(g["ttl"], rec["ttl"])
                g["proxied"] |= typ in ("A", "AAAA", "CNAME") and PROXIED_MARKER in (
                    rec.get("comments") or ""
                )
                if value not in g["values"]:
                    g["values"].append(value)

        for (rel, typ), g in groups.items():
            data = {"type": typ, "ttl": g["ttl"]}
            if typ == "CNAME":
                data["value"] = g["values"][0]
            else:
                data["values"] = g["values"]
            if g["proxied"]:
                data["octodns"] = {"cloudflare": {"proxied": True}}
            zone.add_record(
                Record.new(zone, rel, data, source=self, lenient=lenient), lenient=lenient
            )
        self.log.info("populate: found %d records", len(groups))

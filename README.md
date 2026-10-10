# octodns-notify

**Runs [octoDNS](https://github.com/octodns/octodns) for a zone when a DNS NOTIFY for it arrives.**

Any primary that sends NOTIFY can trigger a sync; it is meant for
[Technitium](https://technitium.com/dns/) with the
[octodns-technitium](https://github.com/Ruakij/octodns-technitium) source, so a zone
change is published to Cloudflare (or any other octoDNS target) within seconds.

## How it works

- A UDP listener answers a NOTIFY for a zone of the octoDNS config with NOERROR and
  queues the zone. Other zones and other queries get REFUSED.
- One worker runs `octodns-sync --config-file FILE [--doit] ZONE` per queued zone,
  one at a time. A zone is queued at most once: NOTIFYs that arrive while its sync
  runs lead to one more sync afterwards.
- A failed sync is logged and retried after 30 seconds.
- All zones are synced at start and then every `--resync` seconds. NOTIFY over UDP
  can get lost, and the target can drift without the primary noticing; the periodic
  run repairs both.
- Logs go to stdout. SIGTERM stops the listener and exits after a running sync.

Wildcard zone names in the config are skipped. There is no TSIG and no source
allowlist: a forged NOTIFY for a known zone only causes one extra sync.

## Technitium

In the zone options of each zone, under Zone Notify, choose "Specified name servers"
and enter the address of this service. Technitium sends NOTIFY over UDP to port 53,
so on Kubernetes the Service maps UDP 53 to the container port 5353 and the
container needs no privileges:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: octodns-notify
spec:
  ports:
    - name: notify
      protocol: UDP
      port: 53
      targetPort: 5353
  selector:
    app: octodns-notify
```

The Service ClusterIP is the address for Technitium.

## octoDNS config

```yaml
providers:
  technitium:
    class: octodns_technitium.TechnitiumSource
    url: https://dns.example.com
    token: env/TECHNITIUM_TOKEN
  cloudflare:
    class: octodns_cloudflare.CloudflareProvider
    token: env/CLOUDFLARE_API_TOKEN
    min_ttl: 60

zones:
  example.com.:
    sources: [technitium]
    targets: [cloudflare]
```

## Usage

```sh
octodns-notify --config-file config.yaml [--doit] [--listen 0.0.0.0] [--port 5353] [--resync 600]
```

| Argument | Default | |
|---|---|---|
| `--config-file` | | octoDNS config, required |
| `--doit` | off | apply the plan; without it syncs are dry runs |
| `--listen` | `0.0.0.0` | address of the listener |
| `--port` | `5353` | UDP port of the listener |
| `--resync` | `600` | seconds between syncs of all zones, `0` disables |

octoDNS (`octodns-sync`) and the providers named in the config must be installed.

```sh
docker run --rm -v ./config.yaml:/config/config.yaml:ro -p 5353:5353/udp \
  -e TECHNITIUM_TOKEN=... -e CLOUDFLARE_API_TOKEN=... \
  ghcr.io/ruakij/octodns-notify --config-file /config/config.yaml --doit
```

The image contains octoDNS, the Cloudflare provider and the Technitium source.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[test]' octodns
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Until the Technitium source is on GitHub, the image builds with a local wheel placed
in the build context:

```sh
docker build --build-arg TECHNITIUM_PLUGIN=/src/octodns_technitium-0.1.0-py3-none-any.whl .
```

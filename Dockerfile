FROM python:3.13-slim AS build
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md LICENSE octodns_notify.py ./
RUN /opt/venv/bin/pip install --no-cache-dir octodns==1.22.0 .

FROM python:3.13-slim AS base
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN useradd --system --uid 65532 --no-create-home nonroot
USER 65532:65532
EXPOSE 5353/udp
ENTRYPOINT ["octodns-notify"]

# The source is plain files in site-packages, not a package: it is built from this
# repo and has no release of its own.
FROM base AS technitium
USER root
RUN pip install --no-cache-dir octodns-cloudflare==1.2.0
COPY octodns_technitium/ /opt/venv/lib/python3.13/site-packages/octodns_technitium/
USER 65532:65532

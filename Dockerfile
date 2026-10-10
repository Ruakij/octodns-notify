FROM python:3.13-slim AS build
# pip needs git for the default git+https plugin source. A local wheel copied into
# the build context works too: --build-arg TECHNITIUM_PLUGIN=/src/<file>.whl
ARG TECHNITIUM_PLUGIN=git+https://github.com/Ruakij/octodns-technitium@main
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir --prefix /install octodns octodns-cloudflare \
    "${TECHNITIUM_PLUGIN}" .

FROM python:3.13-slim
COPY --from=build /install /usr/local
RUN useradd --system --uid 65532 --no-create-home nonroot
USER 65532:65532
EXPOSE 5353/udp
ENTRYPOINT ["octodns-notify"]

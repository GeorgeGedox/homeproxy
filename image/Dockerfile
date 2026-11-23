# HomeProxy image. Build from the repository root:
#   docker build -f image/Dockerfile .
# Every base image and downloaded binary is pinned by version and digest. The
# Renovate and rebuild jobs in M4-7 bump these ARGs.

ARG HAPROXY_IMAGE=haproxy:3.4.7@sha256:f6d2ee87b47192d9cc8c22e88fef405122ff339859644883edc2a7d1827f87b3
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG LEGO_IMAGE=goacme/lego:v5.6.0@sha256:6f1583fc026a455ea46a7a5858c576105bb69c2e2de69db391cab7f14750350c
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490
ARG NODE_IMAGE=node:24.22.2-trixie-slim@sha256:0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d
# Keep PNPM_VERSION and the node image in step with mise.toml.
ARG PNPM_VERSION=12.18.0

# ---- lego and uv: static binaries copied out of their pinned images ----------
FROM ${LEGO_IMAGE} AS lego
FROM ${UV_IMAGE} AS uv

# ---- fetch: s6-overlay and the CrowdSec SPOA bouncer -------------------------
# Runs on the build host architecture and only unpacks files, so an arm64 build
# does not need emulation here.
FROM --platform=$BUILDPLATFORM ${DEBIAN_IMAGE} AS fetch
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG S6_SHA256_NOARCH=5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12
ARG S6_SHA256_X86_64=e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408
ARG S6_SHA256_AARCH64=b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c
ARG SPOA_VERSION=0.4.0
ARG SPOA_SHA256_AMD64=ffe736576d5eaa21367bb5f2db94dabe80c20917fa024f042273289348ecc143
ARG SPOA_SHA256_ARM64=9f4c9d2569d8103cfea69658a761dcd4dd7fe67c95ba35b618797ba7c207f1dd
ARG SPOA_SHA256_TEMPLATE_BAN=ededd9597fd4a95a09abc1d3c563e901680e8d9de428433a25d950f068b261b7
ARG SPOA_SHA256_TEMPLATE_BAN_WITH_CONTACT=da3af2acbb300f1b3a79bdb13b66dfe03150b3afff821103431242e9e6f13cb2
ARG SPOA_SHA256_TEMPLATE_CAPTCHA=b6e198c2e8a031c9dbc7fcb6c508bbb0bd30db15ac4ffaa3156ec125ccd01698

# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /dl
RUN <<'EOF'
set -eu
case "$TARGETARCH" in
  amd64) s6_arch=x86_64;  s6_sha=$S6_SHA256_X86_64;  spoa_sha=$SPOA_SHA256_AMD64 ;;
  arm64) s6_arch=aarch64; s6_sha=$S6_SHA256_AARCH64; spoa_sha=$SPOA_SHA256_ARM64 ;;
  *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;;
esac

s6_url="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"
curl -fsSLO "$s6_url/s6-overlay-noarch.tar.xz"
curl -fsSLO "$s6_url/s6-overlay-$s6_arch.tar.xz"
echo "$S6_SHA256_NOARCH  s6-overlay-noarch.tar.xz" | sha256sum -c -
echo "$s6_sha  s6-overlay-$s6_arch.tar.xz" | sha256sum -c -
mkdir -p /out/s6
tar -C /out/s6 -Jxpf s6-overlay-noarch.tar.xz
tar -C /out/s6 -Jxpf "s6-overlay-$s6_arch.tar.xz"

spoa_url="https://github.com/crowdsecurity/cs-haproxy-spoa-bouncer/releases/download/v${SPOA_VERSION}"
curl -fsSLO "$spoa_url/crowdsec-spoa-bouncer-linux-$TARGETARCH.tgz"
echo "$spoa_sha  crowdsec-spoa-bouncer-linux-$TARGETARCH.tgz" | sha256sum -c -
mkdir -p /out/spoa
tar -C /out/spoa --strip-components=1 -xzf "crowdsec-spoa-bouncer-linux-$TARGETARCH.tgz" \
  "crowdsec-spoa-bouncer-v${SPOA_VERSION}/crowdsec-spoa-bouncer"

# The ban and captcha pages are not in the release archive. Take them from the
# tagged source, one pinned file each.
tpl_url="https://raw.githubusercontent.com/crowdsecurity/cs-haproxy-spoa-bouncer/v${SPOA_VERSION}/templates"
mkdir -p /out/spoa-html
curl -fsSL -o /out/spoa-html/ban.html "$tpl_url/ban.html"
curl -fsSL -o /out/spoa-html/ban-with-contact.html "$tpl_url/ban-with-contact.html"
curl -fsSL -o /out/spoa-html/captcha.html "$tpl_url/captcha.html"
echo "$SPOA_SHA256_TEMPLATE_BAN  /out/spoa-html/ban.html" | sha256sum -c -
echo "$SPOA_SHA256_TEMPLATE_BAN_WITH_CONTACT  /out/spoa-html/ban-with-contact.html" | sha256sum -c -
echo "$SPOA_SHA256_TEMPLATE_CAPTCHA  /out/spoa-html/captcha.html" | sha256sum -c -
EOF

# ---- base: HAProxy image plus Python and the service user --------------------
FROM ${HAPROXY_IMAGE} AS base
# s6-overlay must start as root. Each service drops to the homeproxy user.
# hadolint ignore=DL3002
USER 0
# The Python version comes from Debian trixie, so the venv built in the next
# stage matches the interpreter in the final image. Debian security updates
# replace package versions, so apt packages are not version-pinned.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin homeproxy

# ---- ui: the Nuxt dashboard, built to static files -----------------------------
# Runs on the build host architecture: the output is plain HTML, JS and CSS.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS ui
ARG PNPM_VERSION
RUN npm install --global "pnpm@${PNPM_VERSION}"
WORKDIR /src
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
# The lifecycle scripts need the sources (nuxt prepare), so they run in the build.
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY frontend/ ./
RUN pnpm run build

# ---- venv: backend dependencies and package ----------------------------------
FROM base AS venv
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/homeproxy/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project
COPY backend/homeproxy ./homeproxy
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime ------------------------------------------------------------------
FROM base AS runtime

# s6-overlay, lego, the SPOA bouncer with its pages, and the backend venv.
COPY --from=fetch /out/s6/ /
COPY --from=lego /lego /usr/local/bin/lego
COPY --from=fetch /out/spoa/crowdsec-spoa-bouncer /usr/local/bin/crowdsec-spoa-bouncer
COPY --from=fetch /out/spoa-html/ /var/lib/crowdsec-haproxy-spoa-bouncer/html/
COPY --from=venv /opt/homeproxy/venv /opt/homeproxy/venv
COPY image/placeholder /opt/homeproxy/placeholder
COPY image/scripts/ /etc/s6-overlay/scripts/

# Only HAProxy binds privileged ports, through a file capability, so the
# dashboard and DNS processes never need root. The capability tool is removed
# again after use.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcap2-bin \
 && setcap cap_net_bind_service=+ep /usr/local/sbin/haproxy \
 && apt-get purge -y --auto-remove libcap2-bin \
 && rm -rf /var/lib/apt/lists/*

# The built dashboard. The API serves it from here.
COPY --from=ui /src/.output/public /opt/homeproxy/ui

# Every directory in image/s6-rc.d becomes a service and joins the user bundle,
# so a task adds a service by adding a directory.
COPY image/s6-rc.d/ /tmp/s6-rc.d/
RUN cp -a /tmp/s6-rc.d/. /etc/s6-overlay/s6-rc.d/ \
 && for dir in /tmp/s6-rc.d/*/; do \
      touch "/etc/s6-overlay/user-bundles.d/user/contents.d/$(basename "$dir")"; \
    done \
 && rm -rf /tmp/s6-rc.d

# The first-boot placeholder must be a valid config for this HAProxy build.
RUN HOMEPROXY_PLACEHOLDER_HTTP_PORT=80 haproxy -c -f /opt/homeproxy/placeholder/conf.d/

ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    PYTHONUNBUFFERED=1

VOLUME /data
ENTRYPOINT ["/init"]
# The base image sets a haproxy CMD. s6-overlay would run it as the main program.
CMD []

# ---- dev: the runtime image with a reloading API ------------------------------
# Used by deploy/compose.dev.yaml, which mounts backend/homeproxy at
# /opt/homeproxy/src/homeproxy. PYTHONPATH makes that copy win over the
# installed package, and uvicorn restarts when a file in it changes.
FROM runtime AS dev
COPY --chmod=0755 image/dev/api-run /etc/s6-overlay/s6-rc.d/api/run
ENV PYTHONPATH=/opt/homeproxy/src \
    PYTHONDONTWRITEBYTECODE=1

# ---- ui-dev: Nuxt dev server for deploy/compose.dev.yaml ----------------------
# The compose file mounts frontend/ at /app and runs as the host user, so the
# directories that need to be writable are open to any uid.
FROM ${NODE_IMAGE} AS ui-dev
ARG PNPM_VERSION
RUN npm install --global "pnpm@${PNPM_VERSION}" \
 && mkdir -p /app/node_modules /pnpm-store /home/dev \
 && chmod 0777 /app/node_modules /pnpm-store /home/dev
ENV HOME=/home/dev \
    npm_config_store_dir=/pnpm-store \
    NUXT_TELEMETRY_DISABLED=1
WORKDIR /app

# ---- final: a plain `docker build` produces the runtime image -----------------
FROM runtime AS final
# Links the GHCR package to its repository, so it follows the repository's access.
LABEL org.opencontainers.image.source="https://github.com/GeorgeGedox/homeproxy"

# The Supervisor passes an architecture-specific BUILD_FROM from build.yaml.
# The default is the multi-arch tag, so a plain `docker build` outside Home
# Assistant still resolves to the host's architecture.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

# BUILD_ARCH is the Supervisor's naming (aarch64/amd64). TARGETARCH is supplied
# automatically by BuildKit (arm64/amd64) and is what a plain `docker build`
# provides -- it must be declared without a default, or the automatic value is
# replaced by that default.
ARG BUILD_ARCH=""
ARG TARGETARCH
ARG RESTIC_VERSION=0.19.1

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

ENV PYTHONPATH=/opt/restic-pruner/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# restic ships official static binaries; we pin the version and verify the
# published SHA256 rather than trusting whatever the distro happens to carry.
#
# DL3018: the apk packages are deliberately unpinned. Pinning an apk version that
# a future base image no longer carries breaks the build outright, and none of them
# affects what ends up in the image -- restic's integrity is guaranteed by the
# checksum below, not by the tool that unpacks it.
#
# util-linux-misc supplies `unshare`, which each job uses to give restic its own
# UTS namespace so the repository lock names the job. busybox's applet does not
# carry --map-root-user, so the real one is required.
# hadolint ignore=DL3018
RUN set -eux; \
    arch="${BUILD_ARCH:-${TARGETARCH:-}}"; \
    case "${arch}" in \
      aarch64|arm64) \
        RESTIC_ARCH="arm64"; \
        RESTIC_SHA256="a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465" \
        ;; \
      amd64|x86_64) \
        RESTIC_ARCH="amd64"; \
        RESTIC_SHA256="f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c" \
        ;; \
      *) echo "unsupported or undetected architecture: '${arch}'" >&2; exit 1 ;; \
    esac; \
    apk add --no-cache bzip2 ca-certificates util-linux-misc; \
    wget -q -O /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${RESTIC_ARCH}.bz2"; \
    echo "${RESTIC_SHA256}  /tmp/restic.bz2" | sha256sum -c -; \
    bunzip2 -c /tmp/restic.bz2 > /usr/local/bin/restic; \
    chmod 0755 /usr/local/bin/restic; \
    rm -f /tmp/restic.bz2; \
    restic version; \
    unshare --version

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY src /opt/restic-pruner/src
COPY rootfs /

# Replaces the deprecated `watchdog:` option in config.yaml. The route is exempt
# from the ingress guard precisely so a loopback probe like this one can reach it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8099/api/health || exit 1

LABEL \
  io.hass.name="Restic Pruner" \
  io.hass.description="Scheduled restic forget/prune and repository checks" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64"

# No ENTRYPOINT/CMD here on purpose: the Home Assistant base image already runs
# s6-overlay as PID 1 via `ENTRYPOINT ["/init"]`, and repeating it makes
# s6-overlay-suexec abort with "can only run as pid 1".

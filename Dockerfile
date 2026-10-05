# Tiybai OmniButler - container image
#
# Build (core only, mock + Home Assistant drivers work out of the box):
#   docker build -t tiybai-omnibutler .
#
# Build with optional device drivers. Extras are deliberately NOT baked in
# all at once: each extra pulls a vendor library, so install only the brands
# you own. Pass a comma-separated list via the EXTRAS build arg:
#   docker build --build-arg EXTRAS=miio,tuya -t tiybai-omnibutler .
# Available extras (see pyproject.toml [project.optional-dependencies]):
#   miio       Xiaomi miIO local control (needs the `cryptography` package)
#   tuya       Tuya LAN control (tinytuya)
#   broadlink  Broadlink IR/RF hubs (python-broadlink)
#   midea      Midea appliances (msmart-ng)
#   matter     Matter controller client (websockets)
#   zigbee     Zigbee2MQTT over MQTT (paho-mqtt)
#   dev        test tooling - never needed in a runtime image
# The two vendor-cloud fallback drivers (tuya_cloud, xiaomi_cloud) need
# no extra: they are stdlib-only and selected explicitly at runtime.
#
# Run: the default command is `tob run` (the always-on daemon: schedule
# triggers + device state polling). Which drivers it loads comes from the
# TOB_DRIVER environment variable (mock | homeassistant | miio | tuya |
# broadlink | midea | matter | zigbee2mqtt | tuya_cloud | xiaomi_cloud |
# all, default: mock). Real devices are configured in
# ~/.omnibutler/config.json inside the container - mount a host directory
# over /home/omnibutler/.omnibutler so the config AND the runtime state
# (confirmation queue, audit log) survive restarts. Secrets in the config
# should be "env:VARNAME" references; pass the real values as environment
# variables (see docker-compose.example.yml).
#
# Health check: `tob doctor` exits 0 when nothing is *broken* (warnings
# about not-yet-configured drivers are fine) and 1 on real failures, which
# maps directly onto a Docker HEALTHCHECK. If your deployment intentionally
# leaves a check in a failing state, override the healthcheck in compose.

# ---- builder stage: install the package into a self-contained venv ----
FROM python:3.12-slim AS builder

ARG EXTRAS=""
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY omnibutler ./omnibutler

# EXTRAS empty -> ".", otherwise ".[miio,tuya,...]"
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ -n "$EXTRAS" ]; then /opt/venv/bin/pip install ".[$EXTRAS]"; \
       else /opt/venv/bin/pip install .; fi

# ---- runtime stage ----
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/omnibutler \
    OMNIBUTLER_STATE_DIR=/home/omnibutler/.omnibutler

RUN useradd --create-home --uid 10001 omnibutler \
    && mkdir -p /home/omnibutler/.omnibutler \
    && chown -R omnibutler:omnibutler /home/omnibutler

COPY --from=builder /opt/venv /opt/venv

USER omnibutler
WORKDIR /home/omnibutler

# Persistent config + state (config.json, confirmations queue, audit log).
VOLUME ["/home/omnibutler/.omnibutler"]

# 8765: MCP over HTTP - only when you run a separate `tob mcp --http`
#       process (needs OMNIBUTLER_HTTP_TOKEN; see docker-compose.example.yml)
# 8766: human approvals web page (`tob run --approvals-port 8766`,
#       needs OMNIBUTLER_APPROVALS_TOKEN)
# 8767: phone gateway (`tob run --gateway` or a separate `tob gateway`
#       process, needs OMNIBUTLER_GATEWAY_TOKEN)
EXPOSE 8765 8766 8767

HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD tob doctor >/dev/null 2>&1 || exit 1

ENTRYPOINT ["tob"]
CMD ["run"]

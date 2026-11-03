# Riko's voice: Breeze-TTS-2.cpp (breeze-server), Vulkan build.
# debian:trixie — its Mesa must be new enough for the GPU on the box (an
# RX 9060 XT / RDNA4 needs trixie; bookworm's Mesa is too old). Works on
# NVIDIA too: the container toolkit injects the NVIDIA Vulkan ICD.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# plain RUN (no --mount): must also build with the legacy builder, no BuildKit
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl libvulkan1 mesa-vulkan-drivers libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# pinned release, checksummed (docs/VOICE.md)
ARG BREEZE_VERSION=0.1.6
ARG BREEZE_SHA256=2b5ca9bd3513d46b66aade07922a6c8127d05588d186febb85d0505c1eb28cf8
RUN curl -fL --retry 3 -o /tmp/breeze.tar.gz \
        "https://github.com/HoppouAI/Breeze-TTS-2.cpp/releases/download/v${BREEZE_VERSION}/breeze-tts-2-v${BREEZE_VERSION}-linux-x64-vulkan.tar.gz" \
    && echo "${BREEZE_SHA256}  /tmp/breeze.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/breeze.tar.gz -C /opt \
    && mv "/opt/breeze-tts-2-v${BREEZE_VERSION}-linux-x64-vulkan" /opt/breeze \
    && rm /tmp/breeze.tar.gz

COPY entrypoint.sh /entrypoint.sh

# non-root, but /data is world-writable so `user:` overrides in compose still work
RUN useradd -m -u 1000 breeze \
    && mkdir -p /data/models /data/voices \
    && chmod +x /entrypoint.sh \
    && chmod -R a+w /data
USER breeze

EXPOSE 8137
ENTRYPOINT ["/entrypoint.sh"]

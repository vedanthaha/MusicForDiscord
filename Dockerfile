# ── Stage 1: Lavalink ─────────────────────────────────────────────────────────
FROM eclipse-temurin:21-jre-alpine AS lavalink

WORKDIR /lavalink

# Download Lavalink v4 from the official GitHub release.
# Pin the version for reproducible builds.
ARG LAVALINK_VERSION=4.0.8
RUN wget -q "https://github.com/lavalink-devs/Lavalink/releases/download/${LAVALINK_VERSION}/Lavalink.jar" \
    -O Lavalink.jar

# Download LavaSrc plugin if you need Spotify/Apple Music/Deezer support.
# Uncomment and pin the version you want:
# ARG LAVASRC_VERSION=4.3.0
# RUN mkdir -p plugins && \
#     wget -q "https://github.com/topi314/LavaSrc/releases/download/${LAVASRC_VERSION}/lavasrc-plugin-${LAVASRC_VERSION}.jar" \
#     -O plugins/lavasrc-plugin.jar


# ── Stage 2: Python bot ───────────────────────────────────────────────────────
FROM python:3.12-slim AS bot

WORKDIR /app

# Install OS deps (for aiohttp C-extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create log directory
RUN mkdir -p logs


# ── Final image: combined ─────────────────────────────────────────────────────
# This single-container approach is convenient for small deployments.
# For production, run Lavalink and the bot as separate services.
FROM python:3.12-slim

WORKDIR /app

# Java runtime for Lavalink & audio dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless \
        build-essential \
        ffmpeg \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Copy Lavalink
COPY --from=lavalink /lavalink /lavalink

# Copy Python deps from the bot stage
COPY --from=bot /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=bot /usr/local/bin /usr/local/bin
COPY --from=bot /app /app

# Lavalink config sits next to Lavalink.jar
RUN cp /app/application.yml /lavalink/application.yml

RUN mkdir -p /app/logs /lavalink/logs

# ── Startup script ────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 2333

ENTRYPOINT ["/docker-entrypoint.sh"]

FROM python:3.12-slim

WORKDIR /app

# Install OS dependencies (FFmpeg for audio streaming & build tools for C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create logs directory
RUN mkdir -p logs

# Run bot
CMD ["python", "bot.py"]

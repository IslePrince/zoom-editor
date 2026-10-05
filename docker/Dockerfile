FROM python:3.12-slim-bookworm

# ffmpeg: BtbN static build — has h264_nvenc and drawtext (freetype). The NVIDIA
# runtime injects libnvidia-encode at run time when the GPU is passed through
# with the "video" capability; without it the server falls back to libx264.
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils fonts-dejavu-core && \
    curl -fsSL "$FFMPEG_URL" -o /tmp/ff.tar.xz && \
    tar -xJf /tmp/ff.tar.xz -C /tmp && \
    mv /tmp/ffmpeg-*/bin/ffmpeg /tmp/ffmpeg-*/bin/ffprobe /usr/local/bin/ && \
    rm -rf /tmp/ff.tar.xz /tmp/ffmpeg-* && \
    apt-get purge -y xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY zoom_editor ./zoom_editor
RUN pip install --no-cache-dir .

ENV ZE_DATA_DIR=/data \
    ZE_PORT=8093 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video
EXPOSE 8093
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8093/api/v1/health', timeout=4)" || exit 1
CMD ["zoom-editor"]

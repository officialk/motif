FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 music \
    && mkdir -p /data /soundfonts \
    && chown music:music /data

WORKDIR /srv/music

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY web/ web/

# The soundfont is not baked in (32 MB, licence asks for a link to the author);
# mount a .sf2 read-only at /soundfonts.
ENV MUSIC_HOST=0.0.0.0 \
    MUSIC_DATA_DIR=/data \
    MUSIC_SOUNDFONT_DIR=/soundfonts \
    PYTHONUNBUFFERED=1

USER music
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/genres', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]

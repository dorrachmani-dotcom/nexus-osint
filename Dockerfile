FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System libraries:
#   - WeasyPrint: pango / cairo / gdk-pixbuf / ffi for PDF rendering
#   - fonts: broad Unicode coverage so multilingual feed/reports render
#   - git/curl: fetching OSINT tools that aren't on PyPI
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates unzip \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libcairo2 libffi-dev shared-mime-info \
        fonts-dejavu fonts-noto-core fonts-noto-cjk \
        fonts-noto-hinted \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Headless Chromium for the evidence vault (screenshots).
RUN playwright install --with-deps chromium

# Plug & Play OSINT CLI tools. Best-effort: a failing/renamed package must not
# break the image — the adapters use shutil.which() and skip missing tools.
RUN pip install sherlock-project maigret theHarvester || true \
    && pip install spiderfoot || true \
    && pip install holehe ghunt toutatis onionsearch || true \
    && pip install socialscan h8mail dnstwist yt-dlp || true

# PhoneInfoga is a Go binary (not on PyPI). Fetch a PINNED release tarball and
# drop the binary on PATH — never `curl | bash`, which would execute an
# unpinned remote script at build time (supply-chain risk). Best-effort: a
# network/asset-name failure never breaks the image (the adapter reports the
# tool unavailable via shutil.which() and is skipped).
RUN curl -sSL -o /tmp/phoneinfoga.tar.gz \
        https://github.com/sundowndev/phoneinfoga/releases/download/v2.11.0/phoneinfoga_Linux_x86_64.tar.gz \
    && (cd /tmp && tar -xzf phoneinfoga.tar.gz phoneinfoga \
        && mv phoneinfoga /usr/local/bin/phoneinfoga \
        && chmod +x /usr/local/bin/phoneinfoga) \
    && rm -f /tmp/phoneinfoga.tar.gz \
    || true

# subfinder (ProjectDiscovery) is a Go binary distributed as a prebuilt release.
# Fetch the linux/amd64 archive and drop the binary on PATH; best-effort so a
# network failure never breaks the image (adapter skips it via shutil.which).
RUN curl -sSL -o /tmp/subfinder.zip \
        https://github.com/projectdiscovery/subfinder/releases/download/v2.6.6/subfinder_2.6.6_linux_amd64.zip \
    && (cd /tmp && unzip -o subfinder.zip subfinder -d /usr/local/bin || true) \
    && rm -f /tmp/subfinder.zip \
    || true

# Application code.
COPY nexus ./nexus

# Run as an unprivileged user (defence-in-depth: a process escape isn't root).
# Create the data dir (DB, evidence, exports — overlaid by a volume) and hand
# ownership of the app tree to that user so it can write data/ at runtime.
RUN useradd --create-home --uid 10001 nexus \
    && mkdir -p /app/data \
    && chown -R nexus:nexus /app
USER nexus

EXPOSE 8000

# Container-level health probe (in addition to the one in docker-compose.yml) so
# `docker run` users also get health status.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)" || exit 1

# Bind 0.0.0.0 INSIDE the container so Docker's published-port forwarding can
# reach it. The network exposure boundary is docker-compose.yml, which publishes
# only to 127.0.0.1 (loopback). Running `docker run -p 8000:8000` directly would
# expose it on all interfaces — always publish with the 127.0.0.1: prefix.
CMD ["uvicorn", "nexus.web.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DISPLAY=:99
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    ca-certificates fonts-noto-cjk libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libgtk-3-0 \
    xvfb x11vnc novnc websockify supervisor tini
WORKDIR /app
COPY requirements.lock /app/
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md /app/
COPY cloakup /app/cloakup
RUN pip install --no-cache-dir --no-deps . && useradd -m -u 1000 cloakup \
    && mkdir -p /data /home/cloakup/.cloakbrowser && chown -R cloakup:cloakup /data /home/cloakup
COPY docker/supervisord.conf /etc/supervisor/conf.d/cloakup.conf
COPY docker/entrypoint.py /app/entrypoint.py
USER cloakup
WORKDIR /data
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/entrypoint.py"]

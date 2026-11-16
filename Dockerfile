# One image, reused for every service (bot, sender worker, future scheduler) - each
# docker-compose service just overrides `command`. Keeps one build instead of N near-identical
# Dockerfiles.

FROM python:3.12-slim

# sqlite3: the CLI, for debugging inside the container (`docker compose exec bot sqlite3 ...`).
# curl: same, for poking at things by hand. Neither is a runtime dependency of the app itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Without this, Python block-buffers stdout when it isn't attached to a TTY (true for every
# container) - print() output sits invisible in an internal buffer instead of reaching
# `docker compose logs` until it fills or the process exits. Standard Docker/Python gotcha.
ENV PYTHONUNBUFFERED=1

# Dependencies copied and installed before the rest of the code - this layer only rebuilds when
# requirements.txt actually changes, not on every code edit. On a slow VM connection this is the
# difference between a 5-second rebuild and a 2-minute one.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a real user, not root - a bug or a compromised dependency inside the container doesn't
# get root even inside its own namespace. UID pinned to 1001 to match the VM's `ubuntu` user
# (not the default 1000) - the bind-mounted ./data volume is owned by whichever host user
# deployed this, and a mismatch here means the container silently can't write to it at all
# ("unable to open database file"), while chowning the host dir to match the container instead
# just breaks scp/SSH access to the same files in the other direction. Matching the container to
# the host is the one-sided fix - only the container's UID is arbitrary, the host's isn't.
RUN useradd --uid 1001 --create-home appuser && chown -R appuser:appuser /app
USER appuser

# No CMD/ENTRYPOINT here on purpose - docker-compose.yml sets the actual command per service.

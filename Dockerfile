# rconsole — container image for Render / any Docker host.
#
# Why Docker on Render? The native Python runtime gives you a locked-down
# container you cannot install system packages into. With your own image you
# bake in the tools you want (git, curl, proot, ...) at build time, and the
# per-user VM (`vm install`) works out of the box because proot is preinstalled.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RCONSOLE_DATA_DIR=/data

# proot  : runs the per-user VM without privileges
# tini   : PID 1 that reaps the many short-lived PTY/job children
# the rest: everyday tools for the *host* shell (admins)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      proot tini git curl ca-certificates nano vim-tiny procps less openssh-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# drun: run Docker images without a Docker daemon (udocker, proot mode). A Render service is itself an
# unprivileged container, so a real daemon can never run in it. Not fatal if pip cannot install udocker:
# drun then just says so.
RUN (pip install "udocker==1.3.17" || echo "udocker could not be installed: drun is disabled") \
 && install -m 0755 /app/vmtools/drun /usr/local/bin/drun

# The entrypoint starts as root just long enough to make the (root-owned) data
# volume writable, then runs rconsole as the unprivileged `rconsole` user. Set
# RCONSOLE_RUN_AS_ROOT=1 to keep root (admins get a real-root host shell inside
# the container). The per-user VM gives everyone a root shell regardless.
RUN useradd --create-home --uid 10001 rconsole \
 && mkdir -p /data && chown -R rconsole:rconsole /data /app \
 && chmod +x /app/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','5000'), timeout=4)" || exit 1

ENTRYPOINT ["tini", "--", "/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]

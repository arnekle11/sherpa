# Python minor version and Debian release pinned; patch releases (security fixes) still arrive on rebuild.
FROM python:3.12-slim-trixie
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
# Exact dependency versions first (cached layer); see requirements.lock.
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps . \
 && groupadd -g 1000 sherpa && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin sherpa \
 && mkdir /data && chown sherpa:sherpa /data
USER sherpa
# Every image proves itself before it can run: the whole pipeline against local mocks (no network needed).
RUN sherpa selftest > /tmp/selftest.log 2>&1 || (cat /tmp/selftest.log; exit 1)
# Shown in the startup message, status and web footer, e.g. SHERPA_REV=$(git rev-parse --short HEAD).
ARG SHERPA_REV=""
ENV SHERPA_REV=$SHERPA_REV PYTHONPATH= SHERPA_CONFIG=/config/config.yaml
VOLUME /data
ENTRYPOINT ["sherpa"]
CMD ["run", "-c", "/config/config.yaml", "--fallback", "/data/config.last-good.yaml"]

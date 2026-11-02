FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd -r -u 1000 sherpa && mkdir /data && chown sherpa /data
USER sherpa
ENV PYTHONPATH=
VOLUME /data
ENTRYPOINT ["sherpa"]
CMD ["run", "-c", "/app/config.yaml"]

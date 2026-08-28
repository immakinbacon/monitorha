ARG BUILD_FROM
FROM ${BUILD_FROM}

# The backends speak nothing but HTTP+JSON, so aiohttp is the only dependency.
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages "aiohttp>=3.9,<4"

WORKDIR /opt
COPY app /opt/app

# `services` startup means the Supervisor expects us to stay in the foreground.
CMD ["python3", "-m", "app"]

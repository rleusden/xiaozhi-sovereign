# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG PYTHON_BUILD_IMAGE
ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_BUILD_IMAGE} AS build

RUN apk add --no-cache opus-dev
WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock

FROM ${PYTHON_RUNTIME_IMAGE}

COPY --from=build /usr/lib/libopus.so.0* /usr/lib/
COPY --from=build --chown=65532:65532 /opt/venv /opt/venv
COPY --chown=65532:65532 src /opt/app/src

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/opt/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080 8081
ENTRYPOINT ["/opt/venv/bin/python", "-m", "xiaozhi_gateway.main"]

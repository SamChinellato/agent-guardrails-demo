# Shared multi-stage image for every Python service (REP-03).
# Build context: services/. Base images are passed pinned by digest from versions.env.
ARG PYTHON_BASE
ARG UV_IMAGE

FROM ${UV_IMAGE} AS uv

# Classifier files for the guard (GRD-01), downloaded at build time and verified by
# SHA-256 so the guard makes no network calls at runtime. Empty for other services.
FROM ${PYTHON_BASE} AS model
ARG CLASSIFIER_MODEL=""
ARG CLASSIFIER_REVISION=""
ARG CLASSIFIER_ONNX_SHA256=""
ARG CLASSIFIER_TOKENIZER_SHA256=""
ARG CLASSIFIER_CONFIG_SHA256=""
COPY guard/fetch_model.py /tmp/fetch_model.py
RUN python /tmp/fetch_model.py

FROM ${PYTHON_BASE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY . .
ARG SERVICE
# --frozen: install exactly what uv.lock pins (with hashes); fail if the lock is stale
RUN uv sync --frozen --no-dev --no-editable --package "${SERVICE}"

FROM ${PYTHON_BASE}
ARG SERVICE
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
COPY --from=model /models /models
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SERVICE=${SERVICE}
USER 10001
EXPOSE 8000
# Each service installs a console script named after itself
CMD ["sh", "-c", "exec \"$SERVICE\""]

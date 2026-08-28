FROM rust:1.88-slim AS ingest-builder

WORKDIR /build
COPY rust/das-ingest ./rust/das-ingest
RUN cargo build --release --manifest-path rust/das-ingest/Cargo.toml

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY procurement ./procurement
RUN pip install --no-cache-dir .

COPY --from=ingest-builder /build/rust/das-ingest/target/release/das-ingest /usr/local/bin/das-ingest

RUN useradd --create-home --uid 10001 procurement \
    && mkdir -p /data \
    && chown -R procurement:procurement /app /data

USER procurement
EXPOSE 9200
CMD ["uvicorn", "procurement.app:app", "--host", "0.0.0.0", "--port", "9200", "--no-proxy-headers"]

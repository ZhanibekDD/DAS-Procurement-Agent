FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY procurement ./procurement
COPY Dockerfile /app/Dockerfile
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 procurement \
    && mkdir -p /data \
    && chown -R procurement:procurement /app /data

USER procurement
EXPOSE 9200
CMD ["uvicorn", "procurement.app:app", "--host", "0.0.0.0", "--port", "9200", "--no-proxy-headers"]

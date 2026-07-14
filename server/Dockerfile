FROM python:3.11-slim

WORKDIR /app

COPY server/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends xauth \
    && rm -rf /var/lib/apt/lists/*

COPY server/ .
COPY solver/google_ai_mode/ /app/google-ai-solver/

RUN chmod +x /app/start.sh \
    && mkdir -p /app/output

ENV PYTHONUNBUFFERED=1
ENV ISS_OUTPUT_ROOT=/app/output

EXPOSE 8000 8876

CMD ["/app/start.sh"]

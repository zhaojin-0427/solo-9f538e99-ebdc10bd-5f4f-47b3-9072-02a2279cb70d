FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/data/archives \
    && chown -R appuser:appuser /srv/data
USER appuser

# Archive documents (submissions, supplements, confirmations) persist here.
ENV ARCHIVE_DIR=/srv/data/archives
VOLUME ["/srv/data"]

EXPOSE 8000

CMD ["uvicorn", "app.converter_app:app", "--host", "0.0.0.0", "--port", "8000"]

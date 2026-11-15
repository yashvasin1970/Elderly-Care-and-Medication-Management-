# Container image for Hearth, ready for Google Cloud Run.
#
#   docker build -t hearth .
#   docker run -p 8080:8080 -e HEARTH_DEMO_MODE=1 hearth
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    HEARTH_TRUST_PROXY=1 \
    HEARTH_SECURE_COOKIES=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hearth ./hearth

# Run as an unprivileged user that can only write to the data folder.
RUN useradd --create-home --uid 10001 hearth \
    && mkdir -p /app/instance \
    && chown hearth /app/instance
USER hearth

EXPOSE 8080
# One worker keeps SQLite simple; threads handle concurrent requests.
CMD ["sh", "-c", "exec gunicorn --bind :${PORT} --workers 1 --threads 8 --timeout 60 'hearth:create_app()'"]

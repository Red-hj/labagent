FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000
WORKDIR /app

COPY requirements.txt requirements-workflow.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-workflow.txt \
    && useradd --create-home app
COPY --chown=app:app . .
RUN mkdir -p runtime data/course_notes && chown -R app:app runtime data/course_notes
USER app

EXPOSE 10000
CMD ["python", "-B", "app.py", "serve", "--host", "0.0.0.0"]

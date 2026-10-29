FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/
COPY examples/ /app/examples/
RUN useradd --create-home --uid 10001 deskflow && mkdir -p /app/data && chown -R deskflow:deskflow /app
USER deskflow
CMD ["python", "-m", "uvicorn", "deskflow.api:app", "--host", "0.0.0.0", "--port", "8011"]

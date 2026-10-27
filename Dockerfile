# The gateway as a container: GCP Cloud Run, AWS ECS/App Runner, Azure
# Container Apps, Kubernetes, or plain `docker run` on a lab box.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080

WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ .

RUN useradd --system --no-create-home gateway
USER gateway

EXPOSE 8080
CMD ["python", "server.py"]

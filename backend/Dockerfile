FROM python:3.12-slim

WORKDIR /app
COPY app ./app

ENV AI_WORKFLOW_CONFIG=/app/config/config.json
ENV AI_WORKFLOW_DATA_DIR=/data/ai-workflow
EXPOSE 8000

CMD ["python", "-m", "app.server"]

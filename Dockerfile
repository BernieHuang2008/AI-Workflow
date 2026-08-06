FROM node:22-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

ENV AI_WORKFLOW_CONFIG=/app/config/config.json
ENV AI_WORKFLOW_DATA_DIR=/data/ai-workflow
ENV AI_WORKFLOW_FRONTEND_DIST=/app/frontend/dist

COPY backend/app ./app
COPY config ./config
COPY --from=frontend-build /src/frontend/dist ./frontend/dist

EXPOSE 8000
CMD ["python", "-m", "app.server"]

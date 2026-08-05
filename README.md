# AI Workflow

Internal AI workflow builder and runner.

## Features

- React flowchart editor with draggable nodes and clickable input/output ports.
- Node types: form input, OCR, LLM prompt, Python function, and workflow output.
- API endpoint selection uses public endpoint IDs only. API keys stay in backend configuration or environment variables.
- Runner page submits form input, displays the final product, offers a download, and can expand an in-memory debug trace.
- Workflow definitions and run input/output records are persisted under `/data/ai-workflow`.
- Intermediate node and edge values are returned with the current HTTP response and are not persisted.

## Run With Docker Compose

Create a real config from the example if you want to customize endpoints:

```powershell
Copy-Item .\config\config.example.json .\config\config.json
```

Then start the stack:

```powershell
docker compose up --build
```

Open:

- App: http://localhost:5010
- Backend health through nginx: http://localhost:5010/health

Only the frontend nginx container publishes a host port. The backend listens on port `8000` inside the Docker network and is reached by nginx as `http://backend:8000`.

Docker Compose maps persistent data to the host path:

```text
/data/ai-workflow
```

## Backend Config

`config/config.json`:

```json
{
  "port": 8000,
  "apiEndpoints": [
    {
      "id": "internal-ocr",
      "type": "ocr",
      "label": "Internal OCR",
      "url": "http://ocr-service:9000/ocr",
      "apiKeyEnv": "OCR_API_KEY"
    },
    {
      "id": "internal-llm",
      "type": "llm",
      "label": "Internal LLM",
      "url": "http://llm-gateway:9001/generate",
      "apiKeyEnv": "LLM_API_KEY"
    }
  ]
}
```

The frontend receives only `id`, `type`, `label`, and `description` from `/api/config/catalog`.

## Workflow JSON Shape

```json
{
  "id": "workflow-id",
  "name": "Example workflow",
  "description": "OCR then LLM",
  "nodes": [
    {
      "id": "input-1",
      "type": "input",
      "title": "Form Input",
      "position": { "x": 80, "y": 120 },
      "config": {
        "fields": [
          { "name": "images", "label": "Images", "type": "file" }
        ]
      }
    }
  ],
  "edges": [
    {
      "id": "edge-1",
      "from": { "nodeId": "input-1", "port": "images" },
      "to": { "nodeId": "ocr-1", "port": "imageList" }
    }
  ]
}
```

## API

- `GET /api/config/catalog`
- `GET /api/workflows`
- `POST /api/workflows`
- `GET /api/workflows/{id}`
- `PUT /api/workflows/{id}`
- `DELETE /api/workflows/{id}`
- `POST /api/workflows/{id}/runs`
- `GET /api/workflows/{id}/runs`
- `GET /api/runs/{id}`

Run records persist `input` and final `output`. The `trace` field in the run response is session data for the current browser page.

# AI Workflow

Internal AI workflow builder and runner.

## Features

- React flowchart editor with draggable nodes and clickable input/output ports.
- Node types: form input, OCR, LLM prompt, Python function, and workflow output.
- API endpoint selection uses public endpoint IDs only. API keys are written directly in `config.json`.
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
- Health: http://localhost:5010/health

The app now runs in a single container. The Python backend serves both the API and the built frontend bundle from the same origin.

Docker Compose maps persistent data to the host path:

```text
/data/ai-workflow
```

## Docker Hub CI/CD

GitHub Actions publishes the image from [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml).

Required secrets:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `DOCKERHUB_REPOSITORY`

The workflow tags the image as `latest` and with the current commit SHA.

## Backend Config

`config/config.json`:

```json
{
  "port": 8000,
  "auth": {
    "secretKey": "CHANGE_ME_TO_A_LONG_RANDOM_SECRET",
    "cookieName": "ai_workflow_secret",
    "allowedHost": "ai-workflow.berniehg.top"
  },
  "apiEndpoints": [
    {
      "id": "paddleocr",
      "type": "ocr",
      "provider": "paddleocr",
      "label": "PaddleOCR",
      "url": "https://c8s16af3r0gd36g6.aistudio-app.com/layout-parsing",
      "apiKey": "YOUR_PADDLEOCR_TOKEN"
    },
    {
      "id": "deepseek",
      "type": "llm",
      "provider": "deepseek",
      "label": "DeepSeek",
      "url": "https://api.deepseek.com/v1/chat/completions",
      "model": "deepseek-chat",
      "apiKey": "YOUR_DEEPSEEK_API_KEY"
    }
  ]
}
```

The frontend receives `id`, `type`, `provider`, `label`, and `description` from `/api/config/catalog`.

### Public Auth Gate

Set `auth.secretKey` in `config/config.json`, or set the `AI_WORKFLOW_SECRET` environment variable. When a secret is configured, every `/api/*` route except `/api/auth/status` and `/api/auth/session` requires the matching auth cookie.

The first visit shows a secret-key dialog. A valid secret is stored by the backend as a single host-only cookie named `ai_workflow_secret` by default. The cookie is set with `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, and a long-lived max age, and no `Domain` attribute is written. For `https://ai-workflow.berniehg.top`, that keeps the cookie scoped to `ai-workflow.berniehg.top` only, not `berniehg.top` or sibling subdomains.

You can override:

```powershell
$env:AI_WORKFLOW_SECRET="your-long-random-secret"
$env:AI_WORKFLOW_ALLOWED_HOST="ai-workflow.berniehg.top"
$env:AI_WORKFLOW_AUTH_COOKIE_NAME="ai_workflow_secret"
```

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

## Node Port Rules

- `Python Function`
  - `inputPorts` is edited as a list in the UI and defines the keyword arguments passed into `process(...)`.
  - `outputPorts` is edited as a list in the UI and defines the names you can expose downstream.
  - Your function should return a dictionary, and each key is treated as a named output port.
  - Example:

```python
def process(title, textList):
    return {
        "summary": f"{title}: {textList}",
        "raw": textList
    }
```

- `LLM Prompt`
  - `inputPorts` is edited as a list in the UI.
  - `template` is rendered with `{{portName}}` placeholders.
  - Any incoming value attached to that port can be inserted into the prompt.
  - Example:

```text
Use these OCR results:
{{textList}}

Return a concise answer.
```

  - Inserted values are converted with `str(...)` before replacement.

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

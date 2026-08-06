from __future__ import annotations

import base64
import json
import mimetypes
import os
import ssl
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("AI_WORKFLOW_CONFIG", PROJECT_ROOT / "config" / "config.json"))
DATA_DIR = Path(os.environ.get("AI_WORKFLOW_DATA_DIR", "data"))
FRONTEND_DIST_DIR = Path(os.environ.get("AI_WORKFLOW_FRONTEND_DIST", PROJECT_ROOT / "frontend" / "dist"))
WORKFLOWS_DIR = DATA_DIR / "workflows"
RUNS_DIR = DATA_DIR / "runs"
DEFAULT_ENDPOINTS = [
    {
        "id": "paddleocr",
        "type": "ocr",
        "provider": "paddleocr",
        "label": "PaddleOCR",
        "description": "PaddleOCR layout parsing endpoint.",
        "url": "https://c8s16af3r0gd36g6.aistudio-app.com/layout-parsing",
        "apiKey": "",
    },
    {
        "id": "deepseek",
        "type": "llm",
        "provider": "deepseek",
        "label": "DeepSeek",
        "description": "DeepSeek chat completions endpoint.",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-v4-flash",
        "apiKey": "",
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict[str, Any]:
    fallback = {
        "port": 8000,
        "apiEndpoints": DEFAULT_ENDPOINTS,
    }
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        return {**fallback, **config}
    return fallback


CONFIG = load_config()


def ensure_storage() -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def public_catalog() -> dict[str, Any]:
    endpoints = []
    for item in CONFIG.get("apiEndpoints", []):
        endpoints.append(
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "provider": item.get("provider"),
                "label": item.get("label", item.get("id")),
                "description": item.get("description", ""),
            }
        )
    return {
        "apiEndpoints": endpoints,
        "nodeTypes": [
            {"type": "input", "label": "Form Input"},
            {"type": "ocr", "label": "OCR"},
            {"type": "llm", "label": "LLM Prompt"},
            {"type": "python", "label": "Python Function"},
            {"type": "output", "label": "Output"},
        ],
    }


def safe_id(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_"))


def workflow_path(workflow_id: str) -> Path:
    return WORKFLOWS_DIR / f"{safe_id(workflow_id)}.json"


def run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{safe_id(run_id)}.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


def list_workflows() -> list[dict[str, Any]]:
    ensure_storage()
    workflows = []
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        workflow = read_json(path)
        workflows.append(
            {
                "id": workflow["id"],
                "name": workflow.get("name", "Untitled workflow"),
                "description": workflow.get("description", ""),
                "updatedAt": workflow.get("updatedAt"),
                "nodeCount": len(workflow.get("nodes", [])),
            }
        )
    return workflows


def get_workflow(workflow_id: str) -> dict[str, Any]:
    path = workflow_path(workflow_id)
    if not path.exists():
        raise KeyError("Workflow not found")
    return read_json(path)


def save_workflow(payload: dict[str, Any], workflow_id: str | None = None) -> dict[str, Any]:
    ensure_storage()
    saved = dict(payload)
    saved["id"] = workflow_id or saved.get("id") or str(uuid.uuid4())
    timestamp = now_iso()
    saved.setdefault("createdAt", timestamp)
    saved["updatedAt"] = timestamp
    saved.setdefault("nodes", [])
    saved.setdefault("edges", [])
    saved.setdefault("name", "Untitled workflow")
    write_json(workflow_path(saved["id"]), saved)
    return saved


def endpoint_by_id(endpoint_id: str | None, endpoint_type: str) -> dict[str, Any] | None:
    endpoints = CONFIG.get("apiEndpoints", [])
    typed = [item for item in endpoints if item.get("type") == endpoint_type]
    if endpoint_id:
        for item in endpoints:
            if item.get("id") == endpoint_id:
                return item
    return typed[0] if typed else None


def endpoint_headers(endpoint: dict[str, Any], default_auth_scheme: str = "Bearer") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = endpoint.get("apiKey")
    if api_key:
        header_name = endpoint.get("authHeader", "Authorization")
        auth_scheme = endpoint.get("authScheme", default_auth_scheme)
        headers[header_name] = f"{auth_scheme} {api_key}".strip() if auth_scheme else str(api_key)
    for key, value in endpoint.get("headers", {}).items():
        headers[str(key)] = str(value)
    return headers


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def render_template(template: str, values: dict[str, Any]) -> str:
    rendered = template
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            replacement = json.dumps(value, ensure_ascii=False)
        else:
            replacement = "" if value is None else str(value)
        rendered = rendered.replace("{{" + key + "}}", replacement)
    return rendered


def call_json_endpoint(endpoint: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    url = endpoint.get("url", "")
    if url.startswith("mock://"):
        return {"mock": True, "payload": payload}
    headers = endpoint_headers(endpoint)
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=600) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def call_paddleocr_endpoint(endpoint: dict[str, Any], file_data: str) -> dict[str, Any]:
    url = endpoint.get("url", "")
    headers = endpoint_headers(endpoint, default_auth_scheme="token")
    payload = {
        "file": file_data,
        "fileType": 1,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useTextlineOrientation": False,
        "useChartRecognition": False,
    }
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=300, context=context) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"OCR API request failed with status {exc.code}: {body}") from exc
    return json.loads(raw) if raw else {}


def extract_deepseek_text(response: Any) -> str | dict[str, Any]:
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            content = message.get("content")
            if content is not None:
                return content
        for key in ("output_text", "text", "response", "content"):
            if response.get(key) is not None:
                return response[key]
    return response


def extract_paddleocr_texts(response: Any) -> tuple[list[str], Any]:
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, dict):
            texts = []
            layout_results = result.get("layoutParsingResults")
            if isinstance(layout_results, list):
                for item in layout_results:
                    if not isinstance(item, dict):
                        continue
                    markdown = item.get("markdown")
                    if isinstance(markdown, dict):
                        text = markdown.get("text")
                        if text:
                            texts.append(str(text))
            return texts, result
        return [], result
    return [], response


def image_to_base64(image: Any) -> str | None:
    if isinstance(image, dict):
        for key in ("dataUrl", "dataURL", "base64", "file", "content"):
            value = image.get(key)
            if isinstance(value, str) and value:
                image = value
                break
        else:
            for key in ("path", "filePath"):
                value = image.get(key)
                if isinstance(value, str) and value:
                    path = Path(value)
                    if path.is_file():
                        return base64.b64encode(path.read_bytes()).decode("ascii")
            return None
    if isinstance(image, str):
        if image.startswith("data:") and "," in image:
            return image.split(",", 1)[1]
        path = Path(image)
        if path.is_file():
            return base64.b64encode(path.read_bytes()).decode("ascii")
        return image
    return None


def resolve_ocr_images(inputs: dict[str, Any]) -> list[Any]:
    candidates = inputs.get("imageList")
    if candidates is None:
        candidates = inputs.get("images")
    if candidates is None:
        form = inputs.get("form")
        if isinstance(form, dict):
            candidates = form.get("imageList") or form.get("images")
    return as_list(candidates)


def execute_input_node(node: dict[str, Any], run_inputs: dict[str, Any]) -> dict[str, Any]:
    config = node.get("config", {})
    fields = config.get("fields", [])
    values = {}
    submitted = run_inputs.get(node["id"], run_inputs)
    for field in fields:
        name = field.get("name")
        if name:
            values[name] = submitted.get(name)
    values["form"] = dict(values)
    return values


def execute_ocr_node(node: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    endpoint = endpoint_by_id(node.get("config", {}).get("endpointId"), "ocr")
    images = resolve_ocr_images(inputs)
    if endpoint and not endpoint.get("url", "").startswith("mock://"):
        ocr_texts = []
        raw_results = []
        for image in images:
            file_data = image_to_base64(image)
            if not file_data:
                raise ValueError("OCR node received an image without file data")
            response = call_paddleocr_endpoint(endpoint, file_data)
            texts, raw_result = extract_paddleocr_texts(response)
            ocr_texts.append("\n\n".join(texts))
            raw_results.append(raw_result)
        output: dict[str, Any] = {"ocrResultList": ocr_texts}
        if ocr_texts:
            output["textContent"] = "\n\n".join(text for text in ocr_texts if text)
        if raw_results:
            output["rawResult"] = raw_results[0] if len(raw_results) == 1 else raw_results
        return output
    results = []
    for index, image in enumerate(images):
        label = image.get("name") if isinstance(image, dict) else image
        results.append(f"OCR result {index + 1} from {label or 'image'}")
    return {"ocrResultList": results}


def execute_llm_node(node: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    config = node.get("config", {})
    endpoint = endpoint_by_id(config.get("endpointId"), "llm")
    template = config.get("template", "Summarize:\n{{textList}}")
    input_ports = config.get("inputPorts") or ["textList"]
    prompt_values = {port: inputs.get(port, "") for port in input_ports}
    prompt_values.update(inputs)
    if "textList" not in prompt_values:
        prompt_values["textList"] = inputs.get("ocrResultList") or inputs.get("content") or ""
    prompt = render_template(template, prompt_values)
    if endpoint and not endpoint.get("url", "").startswith("mock://"):
        provider = endpoint.get("provider") or "deepseek"
        if provider == "deepseek":
            payload = {
                "model": config.get("model") or endpoint.get("model") or "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        else:
            payload = {"prompt": prompt, "model": config.get("model"), "config": config}
        response = call_json_endpoint(endpoint, payload)
        text = extract_deepseek_text(response)
        return {"response": text, "prompt": prompt}
    text = f"Mock LLM response for prompt:\n{prompt}"
    return {"response": text, "text": text, "prompt": prompt}


def execute_python_node(node: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    config = node.get("config", {})
    script = config.get("script", "def process(**kwargs):\n    return kwargs\n")
    function_name = config.get("functionName", "process")
    input_ports = config.get("inputPorts") or []
    call_inputs = {port: inputs.get(port) for port in input_ports} if input_ports else dict(inputs)
    namespace: dict[str, Any] = {
        "__builtins__": {
            "len": len,
            "sum": sum,
            "min": min,
            "max": max,
            "str": str,
            "int": int,
            "float": float,
            "list": list,
            "dict": dict,
            "range": range,
            "enumerate": enumerate,
            "json": json,
        }
    }
    exec(script, namespace)
    if function_name not in namespace:
        raise ValueError(f"Function {function_name} was not defined")
    result = namespace[function_name](**call_inputs)
    return result if isinstance(result, dict) else {"result": result}


def execute_output_node(node: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    product_type = node.get("config", {}).get("productType", "text")
    content = inputs.get("content")
    if content is None:
        content = inputs.get("response", inputs.get("text", inputs))
    return {"product": {"type": product_type, "content": content}}


EXECUTORS = {
    "input": execute_input_node,
    "ocr": execute_ocr_node,
    "llm": execute_llm_node,
    "python": execute_python_node,
    "output": execute_output_node,
}


def topo_sort(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_by_id = {node["id"]: node for node in nodes}
    incoming = {node["id"]: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        source = edge["from"]["nodeId"]
        target = edge["to"]["nodeId"]
        if source in node_by_id and target in node_by_id:
            outgoing[source].append(target)
            incoming[target] += 1
    queue = [node_id for node_id, count in incoming.items() if count == 0]
    order = []
    while queue:
        node_id = queue.pop(0)
        order.append(node_by_id[node_id])
        for target in outgoing[node_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if len(order) != len(nodes):
        raise ValueError("Workflow graph contains a cycle")
    return order


def collect_inputs(node_id: str, edges: list[dict[str, Any]], node_outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values = {}
    for edge in edges:
        if edge["to"]["nodeId"] != node_id:
            continue
        source_output = node_outputs.get(edge["from"]["nodeId"], {})
        source_port = edge["from"]["port"]
        target_port = edge["to"]["port"]
        values[target_port] = source_output.get(source_port)
    return values


def execute_workflow(workflow: dict[str, Any], run_inputs: dict[str, Any]) -> dict[str, Any]:
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])
    node_outputs: dict[str, dict[str, Any]] = {}
    trace_nodes = []
    for node in topo_sort(nodes, edges):
        node_inputs = collect_inputs(node["id"], edges, node_outputs)
        executor = EXECUTORS.get(node.get("type"))
        if not executor:
            raise ValueError(f"Unknown node type: {node.get('type')}")
        if node.get("type") == "input":
            output = executor(node, run_inputs)
        else:
            output = executor(node, node_inputs)
        node_outputs[node["id"]] = output
        trace_nodes.append({"nodeId": node["id"], "title": node.get("title"), "input": node_inputs, "output": output})
    edge_trace = []
    for edge in edges:
        edge_trace.append(
            {
                "edgeId": edge.get("id"),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "value": node_outputs.get(edge["from"]["nodeId"], {}).get(edge["from"]["port"]),
            }
        )
    output_nodes = [node for node in nodes if node.get("type") == "output"]
    product = None
    if output_nodes:
        product = node_outputs.get(output_nodes[-1]["id"], {}).get("product")
    return {"product": product, "trace": {"nodes": trace_nodes, "edges": edge_trace}}


def create_run(workflow_id: str, run_inputs: dict[str, Any]) -> dict[str, Any]:
    ensure_storage()
    workflow = get_workflow(workflow_id)
    run_id = str(uuid.uuid4())
    started_at = now_iso()
    try:
        execution = execute_workflow(workflow, run_inputs)
        record = {
            "id": run_id,
            "workflowId": workflow_id,
            "status": "succeeded",
            "input": run_inputs,
            "output": execution["product"],
            "startedAt": started_at,
            "finishedAt": now_iso(),
        }
        write_json(run_path(run_id), record)
        return {**record, "trace": execution["trace"]}
    except Exception as exc:
        record = {
            "id": run_id,
            "workflowId": workflow_id,
            "status": "failed",
            "input": run_inputs,
            "output": None,
            "error": str(exc),
            "startedAt": started_at,
            "finishedAt": now_iso(),
        }
        write_json(run_path(run_id), record)
        raise


def list_runs(workflow_id: str) -> list[dict[str, Any]]:
    ensure_storage()
    items = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        run = read_json(path)
        if run.get("workflowId") == workflow_id:
            items.append(
                {
                    "id": run["id"],
                    "status": run.get("status"),
                    "startedAt": run.get("startedAt"),
                    "finishedAt": run.get("finishedAt"),
                    "output": run.get("output"),
                }
            )
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWorkflow/0.1"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_frontend(self, path: str) -> bool:
        dist_root = FRONTEND_DIST_DIR.resolve()
        if not dist_root.exists():
            return False
        rel_path = unquote(path.lstrip("/"))
        candidate = (dist_root / rel_path).resolve() if rel_path else dist_root / "index.html"
        if rel_path and not candidate.is_relative_to(dist_root):
            return False
        if candidate.is_file():
            self.send_file(candidate)
            return True
        if not Path(rel_path).suffix:
            index_file = dist_root / "index.html"
            if index_file.is_file():
                self.send_file(index_file)
                return True
        return False

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PUT(self) -> None:
        self.route("PUT")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    def route(self, method: str) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            if parsed.path == "/health":
                return self.send_json(200, {"ok": True})
            if parts == ["api", "config", "catalog"] and method == "GET":
                return self.send_json(200, public_catalog())
            if parts == ["api", "workflows"] and method == "GET":
                return self.send_json(200, list_workflows())
            if parts == ["api", "workflows"] and method == "POST":
                return self.send_json(201, save_workflow(self.read_body()))
            if len(parts) == 3 and parts[:2] == ["api", "workflows"]:
                workflow_id = parts[2]
                if method == "GET":
                    return self.send_json(200, get_workflow(workflow_id))
                if method == "PUT":
                    return self.send_json(200, save_workflow(self.read_body(), workflow_id))
                if method == "DELETE":
                    path = workflow_path(workflow_id)
                    if path.exists():
                        path.unlink()
                    return self.send_json(200, {"deleted": True})
            if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "runs":
                workflow_id = parts[2]
                if method == "GET":
                    return self.send_json(200, list_runs(workflow_id))
                if method == "POST":
                    payload = self.read_body()
                    return self.send_json(201, create_run(workflow_id, payload.get("input", payload)))
            if len(parts) == 3 and parts[:2] == ["api", "runs"] and method == "GET":
                return self.send_json(200, read_json(run_path(parts[2])))
            if method == "GET" and self.serve_frontend(parsed.path):
                return
            return self.send_json(404, {"error": "Not found"})
        except KeyError as exc:
            return self.send_json(404, {"error": str(exc)})
        except (ValueError, URLError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    ensure_storage()
    port = int(os.environ.get("PORT", CONFIG.get("port", 8000)))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"AI Workflow backend listening on {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import base64
import builtins as py_builtins
import hmac
import json
import mimetypes
import os
import ssl
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.cookies import SimpleCookie
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
RUN_LOCK = threading.Lock()
DELETED_RUNS: set[str] = set()
TRACE_STRING_LIMIT = 6000
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
AUTH_CONFIG = CONFIG.get("auth", {})
AUTH_SECRET = os.environ.get("AI_WORKFLOW_SECRET") or AUTH_CONFIG.get("secretKey", "")
AUTH_COOKIE_NAME = os.environ.get("AI_WORKFLOW_AUTH_COOKIE_NAME") or AUTH_CONFIG.get("cookieName", "ai_workflow_secret")
AUTH_ALLOWED_HOST = os.environ.get("AI_WORKFLOW_ALLOWED_HOST") or AUTH_CONFIG.get("allowedHost", "ai-workflow.berniehg.top")
AUTH_COOKIE_MAX_AGE = 2_147_483_647


def ensure_storage() -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def auth_enabled() -> bool:
    return bool(AUTH_SECRET)


def host_without_port(host: str) -> str:
    value = host.strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        return value.split("]", 1)[0].lstrip("[")
    return value.rsplit(":", 1)[0]


def parse_cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def make_auth_cookie(secret: str, max_age: int = AUTH_COOKIE_MAX_AGE) -> str:
    cookie = SimpleCookie()
    cookie[AUTH_COOKIE_NAME] = secret
    cookie[AUTH_COOKIE_NAME]["path"] = "/"
    cookie[AUTH_COOKIE_NAME]["max-age"] = str(max_age)
    cookie[AUTH_COOKIE_NAME]["expires"] = "Fri, 31 Dec 9999 23:59:59 GMT"
    cookie[AUTH_COOKIE_NAME]["samesite"] = "Strict"
    cookie[AUTH_COOKIE_NAME]["secure"] = True
    cookie[AUTH_COOKIE_NAME]["httponly"] = True
    return cookie.output(header="").strip()


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
    for attempt in range(12):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(0.05)


def write_run_record(record: dict[str, Any]) -> None:
    with RUN_LOCK:
        if record["id"] in DELETED_RUNS:
            return
        write_json(run_path(record["id"]), record)


def summarize_for_trace(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value:
            return f"<data-url {len(value)} chars>"
        if len(value) > TRACE_STRING_LIMIT:
            return value[:TRACE_STRING_LIMIT] + f"\n... <truncated {len(value) - TRACE_STRING_LIMIT} chars>"
        return value
    if isinstance(value, list):
        return [summarize_for_trace(item) for item in value]
    if isinstance(value, dict):
        return {key: summarize_for_trace(item) for key, item in value.items()}
    return value


def initial_trace(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "nodeId": node["id"],
                "title": node.get("title"),
                "type": node.get("type"),
                "status": "pending",
            }
            for node in workflow.get("nodes", [])
        ],
        "edges": [
            {
                "edgeId": edge.get("id"),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "value": None,
            }
            for edge in workflow.get("edges", [])
        ],
    }


def build_edge_trace(edges: list[dict[str, Any]], node_outputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "edgeId": edge.get("id"),
            "from": edge.get("from"),
            "to": edge.get("to"),
            "value": summarize_for_trace(node_outputs.get(edge["from"]["nodeId"], {}).get(edge["from"]["port"])),
        }
        for edge in edges
    ]


def set_trace_node(
    record: dict[str, Any],
    workflow: dict[str, Any],
    node: dict[str, Any],
    status: str,
    node_inputs: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    error: str | None = None,
    stack: str | None = None,
    node_outputs: dict[str, dict[str, Any]] | None = None,
) -> None:
    trace = record.setdefault("trace", initial_trace(workflow))
    entry = next((item for item in trace.get("nodes", []) if item.get("nodeId") == node["id"]), None)
    if entry is None:
        entry = {"nodeId": node["id"], "title": node.get("title"), "type": node.get("type")}
        trace.setdefault("nodes", []).append(entry)
    entry["status"] = status
    entry["updatedAt"] = now_iso()
    if status == "running":
        entry.setdefault("startedAt", entry["updatedAt"])
    if status in ("succeeded", "failed"):
        entry["finishedAt"] = entry["updatedAt"]
    if node_inputs is not None:
        entry["input"] = summarize_for_trace(node_inputs)
    if output is not None:
        entry["output"] = summarize_for_trace(output)
    if error:
        entry["error"] = error
    if stack:
        entry["traceback"] = stack
        record["traceback"] = stack
    if status == "failed":
        record["failedNode"] = {"nodeId": node["id"], "title": node.get("title"), "type": node.get("type")}
    if node_outputs is not None:
        trace["edges"] = build_edge_trace(workflow.get("edges", []), node_outputs)


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


PADDLEOCR_JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
PADDLEOCR_DEFAULT_MODEL = "PP-OCRv6"
PADDLEOCR_POLL_INTERVAL = 5
PADDLEOCR_JOB_TIMEOUT = 2400
PADDLEOCR_LEGACY_URL_HINTS = ("aistudio-app.com/layout-parsing", "layout-parsing")
OCR_IMAGE_DIR = DATA_DIR / "ocr"
DEFAULT_OCR_OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useTextlineOrientation": False,
}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def resolve_ocr_job_url(config: dict[str, Any], endpoint: dict[str, Any]) -> str:
    """The job API lives on a fixed host; ignore the retired layout-parsing url."""
    url = str(config.get("url") or endpoint.get("url") or "").strip()
    if not url or any(hint in url for hint in PADDLEOCR_LEGACY_URL_HINTS):
        return PADDLEOCR_JOB_URL
    return url


def ocr_job_headers(token: str, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"bearer {token}"} if token else {}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def encode_multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str = "application/octet-stream",
) -> tuple[bytes, str]:
    boundary = "----aiworkflow" + uuid.uuid4().hex
    while boundary.encode("utf-8") in file_bytes:
        boundary = "----aiworkflow" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def open_url_bytes(request: Request, timeout: int = 300, context: Any = None) -> bytes:
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"OCR API request failed with status {exc.code}: {body}") from exc
    except URLError as exc:
        raise ValueError(f"OCR API request failed: {exc.reason}") from exc


def submit_paddleocr_job(api_url: str, token: str, model: str, optional_payload: dict[str, Any], image: Any) -> str:
    context = ssl._create_unverified_context()
    filename, file_bytes, content_type = load_image_bytes(image)
    fields = {"model": model, "optionalPayload": json.dumps(optional_payload)}
    body, multipart_type = encode_multipart(fields, "file", filename, file_bytes, content_type)
    request = Request(api_url, data=body, headers=ocr_job_headers(token, multipart_type), method="POST")
    raw = open_url_bytes(request, timeout=300, context=context)
    return parse_ocr_job_id(json.loads(raw.decode("utf-8")) if raw else {})


def parse_ocr_job_id(response: Any) -> str:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"OCR API did not return job data: {response}")
    job_id = data.get("jobId")
    if not job_id:
        raise ValueError(f"OCR API did not return a job id: {data}")
    return str(job_id)


def poll_paddleocr_job(api_url: str, token: str, job_id: str) -> str:
    context = ssl._create_unverified_context()
    job_url = f"{api_url.rstrip('/')}/{job_id}"
    deadline = time.time() + PADDLEOCR_JOB_TIMEOUT
    while True:
        request = Request(job_url, headers=ocr_job_headers(token), method="GET")
        raw = open_url_bytes(request, timeout=300, context=context)
        data = (json.loads(raw.decode("utf-8")) if raw else {}).get("data") or {}
        state = data.get("state")
        if state == "done":
            result_url = data.get("resultUrl") or {}
            json_url = result_url.get("jsonUrl")
            if not json_url:
                raise ValueError("OCR job finished without a result url")
            return str(json_url)
        if state == "failed":
            raise ValueError(f"OCR job failed: {data.get('errorMsg') or 'unknown error'}")
        if state not in ("pending", "running"):
            raise ValueError(f"OCR job returned an unknown state: {state}")
        if time.time() >= deadline:
            raise ValueError(f"OCR job {job_id} timed out after {PADDLEOCR_JOB_TIMEOUT}s (state: {state})")
        time.sleep(PADDLEOCR_POLL_INTERVAL)


def fetch_paddleocr_result_lines(json_url: str) -> list[dict[str, Any]]:
    context = ssl._create_unverified_context()
    raw = open_url_bytes(Request(json_url, method="GET"), timeout=300, context=context)
    lines = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            lines.append(json.loads(line))
    return lines


def collect_ocr_texts(lines: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        result = line.get("result")
        if not isinstance(result, dict):
            continue
        for item in result.get("ocrResults") or []:
            if not isinstance(item, dict):
                continue
            pruned = item.get("prunedResult")
            rec_texts = pruned.get("rec_texts") if isinstance(pruned, dict) else None
            if isinstance(rec_texts, list) and rec_texts:
                texts.append("\n".join(str(text) for text in rec_texts))
    return texts


def save_ocr_result_images(lines: list[dict[str, Any]], job_id: str) -> list[str]:
    urls: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        result = line.get("result")
        if not isinstance(result, dict):
            continue
        for item in result.get("ocrResults") or []:
            if isinstance(item, dict) and item.get("ocrImage"):
                urls.append(str(item["ocrImage"]))
    if not urls:
        return []
    context = ssl._create_unverified_context()
    target_dir = OCR_IMAGE_DIR / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for index, url in enumerate(urls):
        try:
            content = open_url_bytes(Request(url, method="GET"), timeout=300, context=context)
        except (ValueError, OSError):
            continue
        path = target_dir / f"img_output_{index}.jpg"
        path.write_bytes(content)
        saved.append(str(path))
    return saved


def run_paddleocr_job(config: dict[str, Any], endpoint: dict[str, Any], image: Any) -> dict[str, Any]:
    api_url = resolve_ocr_job_url(config, endpoint)
    token = str(config.get("apiKey") or endpoint.get("apiKey") or "")
    model = str(config.get("model") or endpoint.get("model") or PADDLEOCR_DEFAULT_MODEL)
    optional_payload = dict(DEFAULT_OCR_OPTIONAL_PAYLOAD)
    configured_payload = config.get("optionalPayload")
    if isinstance(configured_payload, dict):
        optional_payload.update(configured_payload)
    for key in list(optional_payload):
        optional_payload[key] = as_bool(config.get(key, optional_payload[key]))
    job_id = submit_paddleocr_job(api_url, token, model, optional_payload, image)
    json_url = poll_paddleocr_job(api_url, token, job_id)
    lines = fetch_paddleocr_result_lines(json_url)
    job_info = {"jobId": job_id, "model": model, "jsonUrl": json_url}
    return {
        "job": job_info,
        "lines": lines,
        "texts": collect_ocr_texts(lines),
        "images": save_ocr_result_images(lines, job_id),
    }


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


def load_image_bytes(image: Any) -> tuple[str, bytes, str]:
    """Return (filename, bytes, content_type) for a node image input.

    Accepts a data URL, raw base64, a dict carrying one of those, or a readable
    local path. The PaddleOCR job API needs bytes for its multipart upload.
    """
    if isinstance(image, dict):
        for key in ("dataUrl", "dataURL", "base64", "file", "content"):
            value = image.get(key)
            if isinstance(value, str) and value:
                return decode_image_value(value, str(image.get("name") or ""), allow_path=False)
        for key in ("path", "filePath"):
            value = image.get(key)
            if isinstance(value, str) and value:
                return read_image_path(value)
        raise ValueError("OCR node received an image without file data")
    if isinstance(image, str):
        return decode_image_value(image, "")
    raise ValueError("OCR node received an unsupported image value")


def decode_image_value(value: Any, name: str = "", allow_path: bool = True) -> tuple[str, bytes, str]:
    if value.startswith("http://") or value.startswith("https://"):
        body = open_url_bytes(Request(value, method="GET"), timeout=300, context=ssl._create_unverified_context())
        filename = name or Path(urlparse(value).path).name or "image.jpg"
        return filename, body, guess_content_type(filename)
    if value.startswith("data:"):
        media_type, _, payload = value[5:].partition(",")
        media_type = media_type.split(";", 1)[0].strip() or "image/jpeg"
        if not name:
            suffix = media_type.split("/", 1)[1] if "/" in media_type else "jpg"
            name = f"image.{suffix}"
        return name, decode_base64_image(payload), media_type
    if allow_path and is_path_like(value):
        return read_image_path(value)
    try:
        return name or "image.jpg", decode_base64_image(value), "image/jpeg"
    except ValueError as exc:
        if value.lower().endswith(IMAGE_SUFFIXES):
            raise ValueError(f"OCR node image file not found: {value}") from exc
        raise


def is_path_like(value: str) -> bool:
    """True only for strings that read as an image path, never for base64 blobs."""
    if not value or len(value) > 4096 or "data:" in value:
        return False
    if any(char in value for char in "\n\r?#|<>*"):
        return False
    if not value.lower().endswith(IMAGE_SUFFIXES):
        return False
    if value.startswith(("data:", "http://", "https://")):
        return False
    for char in value:
        if char in "/\\:":
            return True
    return Path(value).is_file()


def decode_base64_image(payload: str) -> bytes:
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as exc:
        raise ValueError("OCR node received an image that is not valid base64") from exc
    if not looks_like_image(data):
        raise ValueError("OCR node received data that is not a PNG, JPEG, GIF, WebP, BMP or TIFF image")
    return data


IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a",  # GIF
    b"GIF89a",  # GIF
    b"BM",  # BMP
    b"II*\x00",  # TIFF little endian
    b"MM\x00*",  # TIFF big endian
)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")


def looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data.startswith(IMAGE_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def read_image_path(value: str) -> tuple[str, bytes, str]:
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"OCR node image file not found: {value}")
    data = path.read_bytes()
    if not looks_like_image(data):
        raise ValueError(f"OCR node image is not a supported image file: {value}")
    return path.name, data, guess_content_type(path.name)


def guess_content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "image/jpeg"


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
    config = node.get("config", {})
    endpoint = endpoint_by_id(config.get("endpointId"), "ocr")
    images = resolve_ocr_images(inputs)
    if endpoint and not endpoint.get("url", "").startswith("mock://"):
        ocr_texts = []
        jobs = []
        image_paths: list[str] = []
        for image in images:
            job = run_paddleocr_job(config, endpoint, image)
            ocr_texts.append("\n".join(job["texts"]))
            jobs.append(job["job"])
            image_paths.extend(job["images"])
        output: dict[str, Any] = {"ocrResultList": ocr_texts}
        text_content = "\n\n".join(text for text in ocr_texts if text)
        if text_content:
            output["textContent"] = text_content
        if jobs:
            output["rawResult"] = jobs[0] if len(jobs) == 1 else jobs
        if image_paths:
            output["ocrImagePaths"] = image_paths
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
    prompt_values = dict(inputs)
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
    function_name = config.get("functionName") or "process"
    allowed_imports = {"json"}

    def safe_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
        root_name = name.split(".", 1)[0]
        if root_name not in allowed_imports:
            raise ImportError(f"import of '{name}' is not allowed")
        return py_builtins.__import__(name, globals_, locals_, fromlist, level)

    namespace: dict[str, Any] = {
        "__builtins__": {
            "len": len,
            "sum": sum,
            "min": min,
            "max": max,
            "str": str,
            "int": int,
            "float": float,
            "isinstance": isinstance,
            "list": list,
            "dict": dict,
            "range": range,
            "enumerate": enumerate,
            "json": json,
            "__import__": safe_import,
        }
    }
    exec(script, namespace)
    if function_name not in namespace:
        raise ValueError(f"Function {function_name} was not defined")
    result = namespace[function_name](**inputs)
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


def execute_workflow(workflow: dict[str, Any], run_inputs: dict[str, Any], on_node_update: Any | None = None) -> dict[str, Any]:
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])
    node_outputs: dict[str, dict[str, Any]] = {}
    trace_nodes = []
    for node in topo_sort(nodes, edges):
        node_inputs = collect_inputs(node["id"], edges, node_outputs)
        executor = EXECUTORS.get(node.get("type"))
        if not executor:
            raise ValueError(f"Unknown node type: {node.get('type')}")
        if on_node_update:
            on_node_update("running", node, node_inputs, None, None, None, node_outputs)
        try:
            if node.get("type") == "input":
                output = executor(node, run_inputs)
            else:
                output = executor(node, node_inputs)
        except Exception as exc:
            stack = traceback.format_exc()
            if on_node_update:
                on_node_update("failed", node, node_inputs, None, str(exc), stack, node_outputs)
            raise
        node_outputs[node["id"]] = output
        if on_node_update:
            on_node_update("succeeded", node, node_inputs, output, None, None, node_outputs)
        trace_nodes.append(
            {
                "nodeId": node["id"],
                "title": node.get("title"),
                "type": node.get("type"),
                "status": "succeeded",
                "input": summarize_for_trace(node_inputs),
                "output": summarize_for_trace(output),
            }
        )
    edge_trace = build_edge_trace(edges, node_outputs)
    output_nodes = [node for node in nodes if node.get("type") == "output"]
    product = None
    if output_nodes:
        product = node_outputs.get(output_nodes[-1]["id"], {}).get("product")
    return {"product": product, "trace": {"nodes": trace_nodes, "edges": edge_trace}}


def run_status(record: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": record.get("id"),
        "workflowId": record.get("workflowId"),
        "status": record.get("status"),
        "output": record.get("output"),
        "error": record.get("error"),
        "failedNode": record.get("failedNode"),
        "traceback": record.get("traceback"),
        "startedAt": record.get("startedAt"),
        "finishedAt": record.get("finishedAt"),
        "trace": record.get("trace"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def finish_run(record: dict[str, Any], workflow: dict[str, Any], run_inputs: dict[str, Any]) -> None:
    def on_node_update(
        status: str,
        node: dict[str, Any],
        node_inputs: dict[str, Any],
        output: dict[str, Any] | None,
        error: str | None,
        stack: str | None,
        node_outputs: dict[str, dict[str, Any]],
    ) -> None:
        set_trace_node(record, workflow, node, status, node_inputs, output, error, stack, node_outputs)
        write_run_record(record)

    try:
        execution = execute_workflow(workflow, run_inputs, on_node_update=on_node_update)
        record["status"] = "succeeded"
        record["output"] = execution["product"]
        record["trace"]["edges"] = execution["trace"]["edges"]
        record["finishedAt"] = now_iso()
        write_run_record(record)
    except Exception as exc:
        record["status"] = "failed"
        record["output"] = None
        record["error"] = str(exc)
        record["traceback"] = record.get("traceback") or traceback.format_exc()
        record["finishedAt"] = now_iso()
        write_run_record(record)


def create_run(workflow_id: str, run_inputs: dict[str, Any]) -> dict[str, Any]:
    ensure_storage()
    workflow = get_workflow(workflow_id)
    run_id = str(uuid.uuid4())
    DELETED_RUNS.discard(run_id)
    started_at = now_iso()
    record = {
        "id": run_id,
        "workflowId": workflow_id,
        "status": "running",
        "input": run_inputs,
        "output": None,
        "startedAt": started_at,
        "finishedAt": None,
        "trace": initial_trace(workflow),
    }
    write_run_record(record)
    thread = threading.Thread(target=finish_run, args=(record, workflow, run_inputs), daemon=True)
    thread.start()
    return run_status(record)


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
        origin = self.headers.get("Origin", "")
        if self.origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob: data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def origin_allowed(self, origin: str) -> bool:
        if not origin:
            return False
        if auth_enabled():
            return bool(AUTH_ALLOWED_HOST) and origin == f"https://{AUTH_ALLOWED_HOST}"
        return origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")

    def request_host_allowed(self) -> bool:
        if not auth_enabled() or not AUTH_ALLOWED_HOST:
            return True
        return host_without_port(self.headers.get("Host", "")) == AUTH_ALLOWED_HOST.lower()

    def auth_cookie_valid(self) -> bool:
        if not auth_enabled():
            return True
        value = parse_cookie(self.headers.get("Cookie"), AUTH_COOKIE_NAME)
        return bool(value) and hmac.compare_digest(value, AUTH_SECRET)

    def request_authenticated(self) -> bool:
        return self.request_host_allowed() and self.auth_cookie_valid()

    def send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
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
            if parts == ["api", "auth", "status"] and method == "GET":
                host_allowed = self.request_host_allowed()
                return self.send_json(
                    200,
                    {
                        "required": auth_enabled(),
                        "authenticated": (not auth_enabled()) or (host_allowed and self.auth_cookie_valid()),
                        "cookieName": AUTH_COOKIE_NAME,
                        "allowedHost": AUTH_ALLOWED_HOST,
                        "hostAllowed": host_allowed,
                    },
                )
            if parts == ["api", "auth", "session"] and method == "POST":
                if not auth_enabled():
                    return self.send_json(200, {"authenticated": True})
                if not self.request_host_allowed():
                    return self.send_json(403, {"error": "This host is not allowed"})
                payload = self.read_body()
                secret = str(payload.get("secretKey", ""))
                if not secret or not hmac.compare_digest(secret, AUTH_SECRET):
                    return self.send_json(401, {"error": "Invalid secret key"})
                return self.send_json(
                    200,
                    {"authenticated": True},
                    headers={"Set-Cookie": make_auth_cookie(secret)},
                )
            if parts == ["api", "auth", "session"] and method == "DELETE":
                return self.send_json(200, {"authenticated": False}, headers={"Set-Cookie": make_auth_cookie("", max_age=0)})
            if parts and parts[0] == "api" and parts[:2] != ["api", "auth"]:
                if not self.request_host_allowed():
                    return self.send_json(403, {"error": "This host is not allowed"})
                if not self.auth_cookie_valid():
                    return self.send_json(401, {"error": "Unauthorized"})
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
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "status" and method == "GET":
                return self.send_json(200, run_status(read_json(run_path(parts[2]))))
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                if method == "GET":
                    return self.send_json(200, read_json(run_path(parts[2])))
                if method == "DELETE":
                    run_id = parts[2]
                    path = run_path(run_id)
                    with RUN_LOCK:
                        DELETED_RUNS.add(safe_id(run_id))
                        if path.exists():
                            path.unlink()
                    return self.send_json(200, {"deleted": True})
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

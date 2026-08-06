import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API_BASE = "";
const NODE_W = 236;
const PORT_STEP = 30;

const baseFields = [
  { name: "topic", label: "Topic", type: "text" },
  { name: "images", label: "Images", type: "file" }
];

function makeId(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}`;
}

function defaultWorkflow() {
  const input = createNode("input", { x: 80, y: 120 });
  const ocr = createNode("ocr", { x: 390, y: 110 });
  const llm = createNode("llm", { x: 700, y: 110 });
  const output = createNode("output", { x: 1010, y: 120 });
  return {
    name: "New AI Workflow",
    description: "Form input, PaddleOCR, DeepSeek prompt and final output.",
    nodes: [input, ocr, llm, output],
    edges: [
      connect(input.id, "images", ocr.id, "imageList"),
      connect(ocr.id, "ocrResultList", llm.id, "textList"),
      connect(llm.id, "response", output.id, "content")
    ]
  };
}

function createNode(type, position) {
  const id = makeId(type);
  const titles = {
    input: "Form Input",
    ocr: "OCR",
    llm: "LLM Prompt",
    python: "Python Function",
    output: "Output"
  };
  const config = {
    input: { fields: baseFields },
    ocr: { endpointId: "paddleocr" },
    llm: { endpointId: "deepseek", model: "deepseek-chat", template: "Use these OCR results:\n{{textList}}\n\nReturn a concise answer." },
    python: {
      functionName: "process",
      inputPorts: ["value"],
      outputPorts: ["result"],
      script: "def process(**kwargs):\n    return {\"result\": kwargs}\n"
    },
    output: { productType: "text" }
  };
  return { id, type, title: titles[type], position, config: config[type] || {} };
}

function connect(fromNodeId, fromPort, toNodeId, toPort) {
  return { id: makeId("edge"), from: { nodeId: fromNodeId, port: fromPort }, to: { nodeId: toNodeId, port: toPort } };
}

function portsFor(node) {
  if (!node) return { inputs: [], outputs: [] };
  if (node.type === "input") {
    const fields = node.config?.fields || [];
    return { inputs: [], outputs: [...fields.map((field) => field.name), "form"].filter(Boolean) };
  }
  if (node.type === "ocr") return { inputs: ["imageList"], outputs: ["ocrResultList"] };
  if (node.type === "llm") return { inputs: ["textList"], outputs: ["response", "text", "prompt"] };
  if (node.type === "python") {
    return {
      inputs: node.config?.inputPorts || ["value"],
      outputs: node.config?.outputPorts || ["result"]
    };
  }
  if (node.type === "output") return { inputs: ["content", "response", "text"], outputs: ["product"] };
  return { inputs: [], outputs: [] };
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Request failed");
  return payload;
}

function useRoute() {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { page: parts[0] || "list", id: parts[1] };
}

function App() {
  const route = useRoute();
  return (
    <div className="appShell">
      <header className="topbar">
        <a className="brand" href="#/">AI Workflow</a>
        <nav>
          <a href="#/">Workflow List</a>
        </nav>
      </header>
      {route.page === "edit" ? <Editor workflowId={route.id} /> : route.page === "run" ? <Runner workflowId={route.id} /> : <WorkflowList />}
    </div>
  );
}

function WorkflowList() {
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");

  async function load() {
    try {
      setItems(await api("/api/workflows"));
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function createWorkflow() {
    const workflow = await api("/api/workflows", { method: "POST", body: JSON.stringify(defaultWorkflow()) });
    window.location.hash = `#/edit/${workflow.id}`;
  }

  return (
    <main className="page">
      <section className="pageTitle">
        <div>
          <h1>Workflow List</h1>
          <p>Design internal AI workflows, execute them, and inspect run outputs.</p>
        </div>
        <button className="primary" onClick={createWorkflow}>New Workflow</button>
      </section>
      {error && <div className="notice">{error}</div>}
      <section className="workflowGrid">
        {items.map((item) => (
          <article className="workflowCard" key={item.id}>
            <div>
              <h2>{item.name}</h2>
              <p>{item.description || "No description"}</p>
            </div>
            <div className="meta">{item.nodeCount} nodes</div>
            <div className="actions">
              <a className="button" href={`#/edit/${item.id}`}>Edit</a>
              <a className="button primary" href={`#/run/${item.id}`}>Run</a>
            </div>
          </article>
        ))}
        {!items.length && <div className="empty">No workflows yet. Create one to start.</div>}
      </section>
    </main>
  );
}

function Editor({ workflowId }) {
  const [workflow, setWorkflow] = useState(null);
  const [catalog, setCatalog] = useState({ apiEndpoints: [] });
  const [selectedId, setSelectedId] = useState(null);
  const [connecting, setConnecting] = useState(null);
  const [drag, setDrag] = useState(null);
  const [status, setStatus] = useState("");
  const canvasRef = useRef(null);

  useEffect(() => {
    api("/api/config/catalog").then(setCatalog).catch((err) => setStatus(err.message));
    if (workflowId) {
      api(`/api/workflows/${workflowId}`).then((loaded) => {
        setWorkflow(loaded);
        setSelectedId(loaded.nodes?.[0]?.id || null);
      }).catch((err) => setStatus(err.message));
    } else {
      const fresh = defaultWorkflow();
      setWorkflow(fresh);
      setSelectedId(fresh.nodes[0].id);
    }
  }, [workflowId]);

  const selected = workflow?.nodes.find((node) => node.id === selectedId);

  function updateWorkflow(patch) {
    setWorkflow((current) => ({ ...current, ...patch }));
  }

  function updateNode(nodeId, updater) {
    setWorkflow((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === nodeId ? updater(node) : node))
    }));
  }

  function addNode(type) {
    const node = createNode(type, { x: 160 + workflow.nodes.length * 35, y: 140 + workflow.nodes.length * 22 });
    setWorkflow((current) => ({ ...current, nodes: [...current.nodes, node] }));
    setSelectedId(node.id);
  }

  function removeNode(nodeId) {
    setWorkflow((current) => ({
      ...current,
      nodes: current.nodes.filter((node) => node.id !== nodeId),
      edges: current.edges.filter((edge) => edge.from.nodeId !== nodeId && edge.to.nodeId !== nodeId)
    }));
    setSelectedId(null);
  }

  function onPortClick(nodeId, port, direction) {
    if (direction === "out") {
      setConnecting({ nodeId, port });
      return;
    }
    if (connecting && connecting.nodeId !== nodeId) {
      setWorkflow((current) => ({
        ...current,
        edges: [...current.edges, connect(connecting.nodeId, connecting.port, nodeId, port)]
      }));
      setConnecting(null);
    }
  }

  function onMouseDown(event, node) {
    if (event.target.closest(".port")) return;
    const rect = canvasRef.current.getBoundingClientRect();
    setDrag({
      nodeId: node.id,
      dx: event.clientX - rect.left - node.position.x,
      dy: event.clientY - rect.top - node.position.y
    });
    setSelectedId(node.id);
  }

  function onMouseMove(event) {
    if (!drag) return;
    const rect = canvasRef.current.getBoundingClientRect();
    updateNode(drag.nodeId, (node) => ({
      ...node,
      position: {
        x: Math.max(16, event.clientX - rect.left - drag.dx),
        y: Math.max(16, event.clientY - rect.top - drag.dy)
      }
    }));
  }

  async function save() {
    const method = workflow.id ? "PUT" : "POST";
    const path = workflow.id ? `/api/workflows/${workflow.id}` : "/api/workflows";
    const saved = await api(path, { method, body: JSON.stringify(workflow) });
    setWorkflow(saved);
    setStatus("Saved");
    if (!workflow.id) window.location.hash = `#/edit/${saved.id}`;
  }

  if (!workflow) return <main className="page">Loading...</main>;

  return (
    <main className="editor">
      <aside className="palette">
        <h2>Nodes</h2>
        {["input", "ocr", "llm", "python", "output"].map((type) => (
          <button key={type} onClick={() => addNode(type)}>{type}</button>
        ))}
        <div className="divider" />
        <label>
          Name
          <input value={workflow.name} onChange={(event) => updateWorkflow({ name: event.target.value })} />
        </label>
        <label>
          Description
          <textarea value={workflow.description || ""} onChange={(event) => updateWorkflow({ description: event.target.value })} />
        </label>
        <button className="primary" onClick={save}>Save</button>
        {workflow.id && <a className="button" href={`#/run/${workflow.id}`}>Run</a>}
        {status && <div className="status">{status}</div>}
      </aside>
      <section
        className="canvas"
        ref={canvasRef}
        onMouseMove={onMouseMove}
        onMouseUp={() => setDrag(null)}
        onMouseLeave={() => setDrag(null)}
      >
        <EdgeLayer workflow={workflow} />
        {workflow.nodes.map((node) => (
          <FlowNode
            key={node.id}
            node={node}
            selected={node.id === selectedId}
            connecting={connecting}
            onMouseDown={(event) => onMouseDown(event, node)}
            onPortClick={onPortClick}
          />
        ))}
      </section>
      <aside className="inspector">
        <Inspector
          node={selected}
          catalog={catalog}
          onChange={(next) => updateNode(selected.id, () => next)}
          onRemove={() => selected && removeNode(selected.id)}
          edges={workflow.edges}
          onRemoveEdge={(edgeId) => setWorkflow((current) => ({ ...current, edges: current.edges.filter((edge) => edge.id !== edgeId) }))}
        />
      </aside>
    </main>
  );
}

function portPoint(node, port, direction) {
  const ports = portsFor(node)[direction === "in" ? "inputs" : "outputs"];
  const index = Math.max(0, ports.indexOf(port));
  return {
    x: node.position.x + (direction === "out" ? NODE_W : 0),
    y: node.position.y + 76 + index * PORT_STEP
  };
}

function EdgeLayer({ workflow, trace, onEdgeClick }) {
  const nodeMap = new Map(workflow.nodes.map((node) => [node.id, node]));
  return (
    <svg className="edges">
      {workflow.edges.map((edge) => {
        const from = nodeMap.get(edge.from.nodeId);
        const to = nodeMap.get(edge.to.nodeId);
        if (!from || !to) return null;
        const a = portPoint(from, edge.from.port, "out");
        const b = portPoint(to, edge.to.port, "in");
        const mid = Math.max(70, Math.abs(b.x - a.x) / 2);
        const value = trace?.edges?.find((item) => item.edgeId === edge.id)?.value;
        return (
          <g key={edge.id} onClick={() => onEdgeClick?.(edge, value)}>
            <path className="edgePath" d={`M ${a.x} ${a.y} C ${a.x + mid} ${a.y}, ${b.x - mid} ${b.y}, ${b.x} ${b.y}`} />
          </g>
        );
      })}
    </svg>
  );
}

function FlowNode({ node, selected, connecting, onMouseDown, onPortClick }) {
  const { inputs, outputs } = portsFor(node);
  return (
    <article
      className={`flowNode ${selected ? "selected" : ""} ${node.type}`}
      style={{ left: node.position.x, top: node.position.y }}
      onMouseDown={onMouseDown}
    >
      <div className="nodeHead">
        <span>{node.title}</span>
        <small>{node.type}</small>
      </div>
      <div className="portRows">
        <div>
          {inputs.map((port) => (
            <button className="port inputPort" key={port} onClick={() => onPortClick(node.id, port, "in")}>
              <span />{port}
            </button>
          ))}
        </div>
        <div>
          {outputs.map((port) => (
            <button className={`port outputPort ${connecting?.nodeId === node.id && connecting?.port === port ? "active" : ""}`} key={port} onClick={() => onPortClick(node.id, port, "out")}>
              {port}<span />
            </button>
          ))}
        </div>
      </div>
    </article>
  );
}

function Inspector({ node, catalog, onChange, onRemove, edges, onRemoveEdge }) {
  if (!node) return <div className="empty">Select a node to edit it.</div>;
  const config = node.config || {};
  const endpoints = catalog.apiEndpoints.filter((item) => item.type === node.type || (node.type === "llm" && item.type === "llm") || (node.type === "ocr" && item.type === "ocr"));

  function patchNode(patch) {
    onChange({ ...node, ...patch });
  }
  function patchConfig(patch) {
    patchNode({ config: { ...config, ...patch } });
  }

  return (
    <div className="panel">
      <div className="panelHead">
        <h2>Inspector</h2>
        <button className="danger" onClick={onRemove}>Delete</button>
      </div>
      <label>
        Title
        <input value={node.title} onChange={(event) => patchNode({ title: event.target.value })} />
      </label>
      {(node.type === "ocr" || node.type === "llm") && (
        <label>
          API endpoint
          <select value={config.endpointId || ""} onChange={(event) => patchConfig({ endpointId: event.target.value })}>
            {endpoints.map((endpoint) => <option key={endpoint.id} value={endpoint.id}>{endpoint.label}</option>)}
          </select>
        </label>
      )}
      {node.type === "input" && <InputFieldsEditor fields={config.fields || []} onChange={(fields) => patchConfig({ fields })} />}
      {node.type === "llm" && (
        <>
          <label>
            Model
            <input value={config.model || ""} onChange={(event) => patchConfig({ model: event.target.value })} />
          </label>
          <label>
            Prompt template
            <textarea className="code" value={config.template || ""} onChange={(event) => patchConfig({ template: event.target.value })} />
          </label>
        </>
      )}
      {node.type === "python" && (
        <>
          <label>
            Input ports
            <input value={(config.inputPorts || []).join(", ")} onChange={(event) => patchConfig({ inputPorts: csv(event.target.value) })} />
          </label>
          <label>
            Output ports
            <input value={(config.outputPorts || []).join(", ")} onChange={(event) => patchConfig({ outputPorts: csv(event.target.value) })} />
          </label>
          <label>
            Function name
            <input value={config.functionName || "process"} onChange={(event) => patchConfig({ functionName: event.target.value })} />
          </label>
          <label>
            Script
            <textarea className="code tall" value={config.script || ""} onChange={(event) => patchConfig({ script: event.target.value })} />
          </label>
        </>
      )}
      {node.type === "output" && (
        <label>
          Product type
          <select value={config.productType || "text"} onChange={(event) => patchConfig({ productType: event.target.value })}>
            <option value="text">text</option>
            <option value="html">html</option>
            <option value="json">json</option>
          </select>
        </label>
      )}
      <div className="divider" />
      <h3>Ports</h3>
      <div className="portSummary">
        <span>Inputs: {portsFor(node).inputs.join(", ") || "none"}</span>
        <span>Outputs: {portsFor(node).outputs.join(", ") || "none"}</span>
      </div>
      <h3>Connections</h3>
      {edges.filter((edge) => edge.from.nodeId === node.id || edge.to.nodeId === node.id).map((edge) => (
        <button className="edgeChip" key={edge.id} onClick={() => onRemoveEdge(edge.id)}>
          {edge.from.port} to {edge.to.port}
        </button>
      ))}
    </div>
  );
}

function csv(value) {
  return value.split(",").map((part) => part.trim()).filter(Boolean);
}

function InputFieldsEditor({ fields, onChange }) {
  function update(index, patch) {
    onChange(fields.map((field, itemIndex) => (itemIndex === index ? { ...field, ...patch } : field)));
  }
  return (
    <div className="fieldEditor">
      <h3>Form fields</h3>
      {fields.map((field, index) => (
        <div className="fieldRow" key={`${field.name}-${index}`}>
          <input value={field.name} placeholder="name" onChange={(event) => update(index, { name: event.target.value })} />
          <input value={field.label || ""} placeholder="label" onChange={(event) => update(index, { label: event.target.value })} />
          <select value={field.type || "text"} onChange={(event) => update(index, { type: event.target.value })}>
            <option value="text">text</option>
            <option value="textarea">textarea</option>
            <option value="number">number</option>
            <option value="file">file</option>
          </select>
          <button onClick={() => onChange(fields.filter((_, itemIndex) => itemIndex !== index))}>Remove</button>
        </div>
      ))}
      <button onClick={() => onChange([...fields, { name: "value", label: "Value", type: "text" }])}>Add field</button>
    </div>
  );
}

function Runner({ workflowId }) {
  const [workflow, setWorkflow] = useState(null);
  const [formValues, setFormValues] = useState({});
  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [debugOpen, setDebugOpen] = useState(false);
  const [edgeData, setEdgeData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api(`/api/workflows/${workflowId}`).then(setWorkflow).catch((err) => setError(err.message));
    api(`/api/workflows/${workflowId}/runs`).then(setRuns).catch(() => {});
  }, [workflowId]);

  const inputNodes = useMemo(() => workflow?.nodes.filter((node) => node.type === "input") || [], [workflow]);

  function setField(nodeId, name, value) {
    setFormValues((current) => ({ ...current, [nodeId]: { ...(current[nodeId] || {}), [name]: value } }));
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    try {
      const result = await api(`/api/workflows/${workflowId}/runs`, { method: "POST", body: JSON.stringify({ input: formValues }) });
      setRun(result);
      setDebugOpen(false);
      setRuns(await api(`/api/workflows/${workflowId}/runs`));
    } catch (err) {
      setError(err.message);
    }
  }

  if (!workflow) return <main className="page">Loading...</main>;

  return (
    <main className="runner page">
      <section className="pageTitle">
        <div>
          <h1>{workflow.name}</h1>
          <p>{workflow.description || "Execute this workflow and inspect the session trace."}</p>
        </div>
        <a className="button" href={`#/edit/${workflow.id}`}>Edit</a>
      </section>
      {error && <div className="notice">{error}</div>}
      <section className="runLayout">
        <form className="runForm" onSubmit={submit}>
          <h2>Input</h2>
          {inputNodes.map((node) => (
            <div key={node.id} className="inputGroup">
              <h3>{node.title}</h3>
              {(node.config?.fields || []).map((field) => (
                <RunField key={field.name} field={field} value={formValues[node.id]?.[field.name] || ""} onChange={(value) => setField(node.id, field.name, value)} />
              ))}
            </div>
          ))}
          <button className="primary" type="submit">Submit</button>
        </form>
        <section className="resultPane">
          <div className="panelHead">
            <h2>Output</h2>
            {run?.output && <DownloadButton output={run.output} />}
          </div>
          <ProductView output={run?.output} />
          <button className="button" disabled={!run} onClick={() => setDebugOpen((open) => !open)}>Debug trace</button>
        </section>
      </section>
      {debugOpen && run && (
        <section className="debugPane">
          <div className="debugCanvas">
            <EdgeLayer workflow={workflow} trace={run.trace} onEdgeClick={(edge, value) => setEdgeData({ edge, value })} />
            {workflow.nodes.map((node) => <FlowNode key={node.id} node={node} selected={false} onMouseDown={() => {}} onPortClick={() => {}} />)}
          </div>
          <aside className="edgeData">
            <h2>Edge Data</h2>
            <pre>{edgeData ? JSON.stringify(edgeData, null, 2) : "Click an edge to inspect the value from this session."}</pre>
          </aside>
        </section>
      )}
      <section className="history">
        <h2>Persisted Runs</h2>
        {runs.map((item) => <div className="historyRow" key={item.id}>{item.status} - {item.startedAt}</div>)}
      </section>
    </main>
  );
}

function RunField({ field, value, onChange }) {
  if (field.type === "textarea") {
    return <label>{field.label || field.name}<textarea value={value} onChange={(event) => onChange(event.target.value)} /></label>;
  }
  if (field.type === "number") {
    return <label>{field.label || field.name}<input type="number" value={value} onChange={(event) => onChange(Number(event.target.value))} /></label>;
  }
  if (field.type === "file") {
    return (
      <label>
        {field.label || field.name}
        <input type="file" multiple onChange={async (event) => onChange(await readFiles(event.target.files))} />
      </label>
    );
  }
  return <label>{field.label || field.name}<input value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

function readFiles(fileList) {
  return Promise.all(Array.from(fileList).map((file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, size: file.size, type: file.type, dataUrl: reader.result });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  })));
}

function ProductView({ output }) {
  if (!output) return <div className="empty">Run the workflow to see the product.</div>;
  const { type, content } = output;
  if (type === "html") return <iframe className="htmlPreview" title="Workflow output" srcDoc={String(content || "")} />;
  if (type === "json") return <pre>{JSON.stringify(content, null, 2)}</pre>;
  return <pre>{typeof content === "string" ? content : JSON.stringify(content, null, 2)}</pre>;
}

function DownloadButton({ output }) {
  const text = output.type === "json" ? JSON.stringify(output.content, null, 2) : String(output.content ?? "");
  const blob = new Blob([text], { type: output.type === "html" ? "text/html" : "text/plain" });
  const url = URL.createObjectURL(blob);
  const extension = output.type === "html" ? "html" : output.type === "json" ? "json" : "txt";
  return <a className="button primary" href={url} download={`workflow-output.${extension}`}>Download</a>;
}

createRoot(document.getElementById("root")).render(<App />);

import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API_BASE = "";
const NODE_W = 236;
const PORT_STEP = 30;
const AUTH_EXPIRED_EVENT = "ai-workflow-auth-expired";
const terminalRunStatuses = new Set(["succeeded", "failed"]);

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
    llm: {
      endpointId: "deepseek",
      model: "deepseek-chat",
      inputPorts: ["textList"],
      template: "Use these OCR results:\n{{textList}}\n\nReturn a concise answer."
    },
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

function nodeTrace(trace, nodeId) {
  return trace?.nodes?.find((item) => item.nodeId === nodeId);
}

function statusLabel(status) {
  if (status === "succeeded") return "Done";
  if (status === "running") return "Running";
  if (status === "failed") return "Failed";
  return "Pending";
}

function formatLocalTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function portsFor(node) {
  if (!node) return { inputs: [], outputs: [] };
  if (node.type === "input") {
    const fields = node.config?.fields || [];
    return { inputs: [], outputs: [...fields.map((field) => field.name), "form"].filter(Boolean) };
  }
  if (node.type === "ocr") return { inputs: ["imageList"], outputs: ["ocrResultList"] };
  if (node.type === "llm") {
    return {
      inputs: node.config?.inputPorts || ["textList"],
      outputs: ["response", "text", "prompt"]
    };
  }
  if (node.type === "python") {
    return {
      inputs: node.config?.inputPorts || ["value"],
      outputs: node.config?.outputPorts || ["result"]
    };
  }
  if (node.type === "output") return { inputs: ["content", "response", "text"], outputs: ["product"] };
  return { inputs: [], outputs: [] };
}

function syncNodeEdges(edges, previousNode, nextNode) {
  if (!previousNode || !nextNode) return edges;
  const previousPorts = portsFor(previousNode);
  const nextPorts = portsFor(nextNode);
  const stablePorts = new Set(["form"]);
  const mapPorts = (previousList, nextList) => {
    const mapped = new Map();
    const nextSequence = nextList.filter((port) => !stablePorts.has(port));
    let nextIndex = 0;
    previousList.forEach((port) => {
      if (stablePorts.has(port)) {
        if (nextList.includes(port)) mapped.set(port, port);
        return;
      }
      mapped.set(port, nextSequence[nextIndex]);
      nextIndex += 1;
    });
    return mapped;
  };
  const inputMap = mapPorts(previousPorts.inputs, nextPorts.inputs);
  const outputMap = mapPorts(previousPorts.outputs, nextPorts.outputs);

  return edges.reduce((acc, edge) => {
    let nextEdge = edge;
    if (edge.to.nodeId === previousNode.id) {
      const mappedPort = inputMap.get(edge.to.port);
      if (mappedPort === undefined) return acc;
      if (mappedPort !== edge.to.port) {
        nextEdge = { ...nextEdge, to: { ...nextEdge.to, port: mappedPort } };
      }
    }
    if (edge.from.nodeId === previousNode.id) {
      const mappedPort = outputMap.get(edge.from.port);
      if (mappedPort === undefined) return acc;
      if (mappedPort !== edge.from.port) {
        nextEdge = { ...nextEdge, from: { ...nextEdge.from, port: mappedPort } };
      }
    }
    acc.push(nextEdge);
    return acc;
  }, []);
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT));
    }
    throw new Error(payload.error || "Request failed");
  }
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

function AuthGate() {
  const [auth, setAuth] = useState({ checking: true, required: false, authenticated: false, hostAllowed: true });

  async function checkAuth() {
    try {
      const response = await fetch(`${API_BASE}/api/auth/status`, { credentials: "same-origin" });
      const payload = await response.json();
      setAuth({ checking: false, ...payload });
    } catch (err) {
      setAuth({ checking: false, required: true, authenticated: false, hostAllowed: true, error: err.message });
    }
  }

  useEffect(() => {
    checkAuth();
    const onExpired = () => setAuth((current) => ({ ...current, required: true, authenticated: false }));
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  if (auth.checking) {
    return <main className="page">Loading...</main>;
  }

  if (auth.required && !auth.authenticated) {
    return <AuthDialog auth={auth} onAuthenticated={checkAuth} />;
  }

  return <App />;
}

function AuthDialog({ auth, onAuthenticated }) {
  const [secretKey, setSecretKey] = useState("");
  const [error, setError] = useState(auth.error || "");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/auth/session`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secretKey })
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || "Invalid secret key");
      setSecretKey("");
      await onAuthenticated();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="authBackdrop">
      <form className="authDialog" onSubmit={submit} role="dialog" aria-modal="true" aria-labelledby="authTitle">
        <h1 id="authTitle">AI Workflow</h1>
        <label>
          Secret key
          <input
            autoFocus
            type="password"
            value={secretKey}
            onChange={(event) => setSecretKey(event.target.value)}
            autoComplete="current-password"
            disabled={!auth.hostAllowed || submitting}
          />
        </label>
        {!auth.hostAllowed && <div className="notice">Please open this app from {auth.allowedHost}.</div>}
        {error && <div className="notice">{error}</div>}
        <button className="primary" type="submit" disabled={!auth.hostAllowed || submitting || !secretKey}>
          {submitting ? "Checking..." : "Unlock"}
        </button>
      </form>
    </div>
  );
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
              <a className="button primary" href={`#/run/${item.id}`}>Run / Records</a>
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
  const [saveState, setSaveState] = useState("idle");
  const canvasRef = useRef(null);

  useEffect(() => {
    api("/api/config/catalog").then(setCatalog).catch((err) => setStatus(err.message));
    if (workflowId) {
      api(`/api/workflows/${workflowId}`).then((loaded) => {
        setWorkflow(loaded);
        setSelectedId(loaded.nodes?.[0]?.id || null);
        setSaveState("idle");
      }).catch((err) => setStatus(err.message));
    } else {
      const fresh = defaultWorkflow();
      setWorkflow(fresh);
      setSelectedId(fresh.nodes[0].id);
      setSaveState("idle");
    }
  }, [workflowId]);

  const selected = workflow?.nodes.find((node) => node.id === selectedId);

  function markDirty() {
    setSaveState("dirty");
  }

  function updateWorkflow(patch) {
    setWorkflow((current) => ({ ...current, ...patch }));
    markDirty();
  }

  function updateNode(nodeId, updater) {
    setWorkflow((current) => ({
      ...current,
      nodes: current.nodes.map((node) => (node.id === nodeId ? updater(node) : node))
    }));
    markDirty();
  }

  function updateNodeWithEdgeSync(nodeId, updater) {
    setWorkflow((current) => {
      const previousNode = current.nodes.find((node) => node.id === nodeId);
      if (!previousNode) return current;
      const nextNode = updater(previousNode);
      return {
        ...current,
        nodes: current.nodes.map((node) => (node.id === nodeId ? nextNode : node)),
        edges: syncNodeEdges(current.edges, previousNode, nextNode)
      };
    });
    markDirty();
  }

  function addNode(type) {
    const node = createNode(type, { x: 160 + workflow.nodes.length * 35, y: 140 + workflow.nodes.length * 22 });
    setWorkflow((current) => ({ ...current, nodes: [...current.nodes, node] }));
    setSelectedId(node.id);
    markDirty();
  }

  function removeNode(nodeId) {
    setWorkflow((current) => ({
      ...current,
      nodes: current.nodes.filter((node) => node.id !== nodeId),
      edges: current.edges.filter((edge) => edge.from.nodeId !== nodeId && edge.to.nodeId !== nodeId)
    }));
    setSelectedId(null);
    markDirty();
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
    setSaveState("saving");
    const method = workflow.id ? "PUT" : "POST";
    const path = workflow.id ? `/api/workflows/${workflow.id}` : "/api/workflows";
    try {
      const saved = await api(path, { method, body: JSON.stringify(workflow) });
      setWorkflow(saved);
      setSaveState("saved");
      setStatus("Saved");
      if (!workflow.id) window.location.hash = `#/edit/${saved.id}`;
    } catch (err) {
      setSaveState("dirty");
      setStatus(err.message);
    }
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
        <button className={`primary ${saveState === "saved" ? "success" : ""}`} onClick={save}>
          {saveState === "saving" ? "Saving..." : "Save"}
        </button>
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
          onChange={(next) => updateNodeWithEdgeSync(selected.id, () => next)}
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

function StatusBadge({ status }) {
  if (!status) return null;
  return <span className={`nodeStatus ${status}`} title={statusLabel(status)} aria-label={statusLabel(status)} />;
}

function FlowNode({ node, selected, connecting, status, onMouseDown, onPortClick }) {
  const { inputs, outputs } = portsFor(node);
  return (
    <article
      className={`flowNode ${selected ? "selected" : ""} ${node.type} ${status ? `status-${status}` : ""}`}
      style={{ left: node.position.x, top: node.position.y }}
      onMouseDown={onMouseDown}
    >
      <div className="nodeHead">
        <div className="nodeText">
          <span>{node.title}</span>
          <small>{node.type}</small>
        </div>
        <StatusBadge status={status} />
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
          <PortListEditor
            title="Input ports"
            ports={config.inputPorts || ["textList"]}
            defaultItem="textList"
            onChange={(inputPorts) => patchConfig({ inputPorts })}
          />
          <label>
            Model
            <input value={config.model || ""} onChange={(event) => patchConfig({ model: event.target.value })} />
          </label>
          <label>
            Prompt template
            <textarea className="code" value={config.template || ""} onChange={(event) => patchConfig({ template: event.target.value })} />
          </label>
          <div className="helperText">
            Use <code>{'{{portName}}'}</code> to insert incoming values from any input port.
            Example: <code>{'Combine {{topic}} and {{textList}} into one answer.'}</code>
            Inserted values are stringified with <code>str(...)</code> before template rendering.
          </div>
        </>
      )}
      {node.type === "python" && (
        <>
          <PortListEditor
            title="Input ports"
            ports={config.inputPorts || ["value"]}
            defaultItem="value"
            onChange={(inputPorts) => patchConfig({ inputPorts })}
          />
          <PortListEditor
            title="Output ports"
            ports={config.outputPorts || ["result"]}
            defaultItem="result"
            onChange={(outputPorts) => patchConfig({ outputPorts })}
          />
          <label>
            Function name
            <input value={config.functionName || "process"} onChange={(event) => patchConfig({ functionName: event.target.value })} />
          </label>
          <label>
            Script
            <textarea className="code tall" value={config.script || ""} onChange={(event) => patchConfig({ script: event.target.value })} />
          </label>
          <div className="helperText">
            Input ports are passed to <code>process(...)</code> as keyword arguments. Return a dictionary whose keys match the output port names.
            Example: <code>{'def process(title, textList): return {"summary": f"{title}: {textList}"}'}</code>
          </div>
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

function PortListEditor({ title, ports, onChange, defaultItem }) {
  function update(index, nextValue) {
    onChange(ports.map((port, itemIndex) => (itemIndex === index ? nextValue : port)));
  }

  return (
    <div className="fieldEditor">
      <h3>{title}</h3>
      {ports.map((port, index) => (
        <div className="portRow" key={index}>
          <input value={port} placeholder="port name" onChange={(event) => update(index, event.target.value)} />
          <button type="button" onClick={() => onChange(ports.filter((_, itemIndex) => itemIndex !== index))}>Remove</button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...ports, defaultItem])}>
        Add port
      </button>
    </div>
  );
}

function InputFieldsEditor({ fields, onChange }) {
  function update(index, patch) {
    onChange(fields.map((field, itemIndex) => (itemIndex === index ? { ...field, ...patch } : field)));
  }
  return (
    <div className="fieldEditor">
      <h3>Form fields</h3>
      {fields.map((field, index) => (
        <div className="fieldRow" key={index}>
          <input value={field.name} placeholder="name" onChange={(event) => update(index, { name: event.target.value })} />
          <input value={field.label || ""} placeholder="label" onChange={(event) => update(index, { label: event.target.value })} />
          <select value={field.type || "text"} onChange={(event) => update(index, { type: event.target.value })}>
            <option value="text">text</option>
            <option value="textarea">textarea</option>
            <option value="number">number</option>
            <option value="file">file</option>
          </select>
          <button type="button" onClick={() => onChange(fields.filter((_, itemIndex) => itemIndex !== index))}>Remove</button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...fields, { name: "value", label: "Value", type: "text" }])}>Add field</button>
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
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    api(`/api/workflows/${workflowId}`).then(setWorkflow).catch((err) => setError(err.message));
    api(`/api/workflows/${workflowId}/runs`).then(setRuns).catch(() => {});
  }, [workflowId]);

  const inputNodes = useMemo(() => workflow?.nodes.filter((node) => node.type === "input") || [], [workflow]);
  const isRunning = run?.status === "running";

  useEffect(() => {
    if (!run?.id || terminalRunStatuses.has(run.status)) return undefined;
    let cancelled = false;

    async function pollStatus() {
      try {
        const next = await api(`/api/runs/${run.id}/status`);
        if (cancelled) return;
        setRun(next);
        if (terminalRunStatuses.has(next.status)) {
          setRuns(await api(`/api/workflows/${workflowId}/runs`));
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }

    const timer = window.setInterval(pollStatus, 800);
    pollStatus();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [run?.id, run?.status, workflowId]);

  function setField(nodeId, name, value) {
    setFormValues((current) => ({ ...current, [nodeId]: { ...(current[nodeId] || {}), [name]: value } }));
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);
    try {
      setEdgeData(null);
      setDebugOpen(true);
      const result = await api(`/api/workflows/${workflowId}/runs`, { method: "POST", body: JSON.stringify({ input: formValues }) });
      setRun(result);
      setRuns(await api(`/api/workflows/${workflowId}/runs`));
    } catch (err) {
      setError(err.message);
    } finally {
      setIsSubmitting(false);
    }
  }

  async function openRun(runId) {
    setError("");
    setEdgeData(null);
    try {
      const next = await api(`/api/runs/${runId}/status`);
      setRun(next);
      setDebugOpen(true);
    } catch (err) {
      setError(err.message);
    }
  }

  async function deleteRun(runId) {
    if (!window.confirm("Delete this run record?")) return;
    setError("");
    try {
      await api(`/api/runs/${runId}`, { method: "DELETE" });
      setRuns((current) => current.filter((item) => item.id !== runId));
      if (run?.id === runId) {
        setRun(null);
        setDebugOpen(false);
        setEdgeData(null);
      }
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
          <button className="primary" type="submit" disabled={isSubmitting || isRunning}>
            {isSubmitting || isRunning ? (
              <>
                <span className="spinner" aria-hidden="true" />
                {isRunning ? "Running..." : "Submitting..."}
              </>
            ) : "Submit"}
          </button>
        </form>
        <section className="resultPane">
          <div className="panelHead">
            <h2>Output</h2>
            {run?.status && <span className={`runStatus ${run.status}`}>{statusLabel(run.status)}</span>}
            {run?.output && <DownloadButton output={run.output} />}
          </div>
          <ProductView output={run?.output} />
          <button className="button" disabled={!run} onClick={() => setDebugOpen((open) => !open)}>
            {debugOpen ? "Hide trace" : "Show trace"}
          </button>
        </section>
      </section>
      {run && (debugOpen || isRunning) && (
        <section className="debugPane">
          <div className="debugCanvas">
            <EdgeLayer workflow={workflow} trace={run.trace} onEdgeClick={(edge, value) => setEdgeData({ edge, value })} />
            {workflow.nodes.map((node) => (
              <FlowNode
                key={node.id}
                node={node}
                selected={false}
                status={nodeTrace(run.trace, node.id)?.status || "pending"}
                onMouseDown={() => {}}
                onPortClick={() => {}}
              />
            ))}
          </div>
          <RunSteps workflow={workflow} run={run} edgeData={edgeData} />
        </section>
      )}
      <section className="history">
        <h2>History Runs</h2>
        {runs.map((item) => (
          <div className={`historyRow ${run?.id === item.id ? "selected" : ""}`} key={item.id}>
            <button type="button" className="historyOpen" onClick={() => openRun(item.id)}>
              <span className={`runStatus ${item.status}`}>{statusLabel(item.status)}</span>
              <span>{formatLocalTime(item.startedAt)}</span>
            </button>
            <button type="button" className="danger historyDelete" onClick={() => deleteRun(item.id)}>
              Delete
            </button>
          </div>
        ))}
      </section>
    </main>
  );
}

function RunSteps({ workflow, run, edgeData }) {
  return (
    <aside className="edgeData runLog">
      <div className="panelHead">
        <h2>Run Log</h2>
        <span className={`runStatus ${run.status}`}>{statusLabel(run.status)}</span>
      </div>
      <div className="stepList">
        {workflow.nodes.map((node) => {
          const item = nodeTrace(run.trace, node.id) || { status: "pending" };
          const status = item.status || "pending";
          return (
            <div className={`stepRow ${status}`} key={node.id}>
              <StatusBadge status={status} />
              <div>
                <strong>{node.title}</strong>
                <small>{statusLabel(status)}</small>
              </div>
            </div>
          );
        })}
      </div>
      {(run.error || run.traceback) && (
        <>
          <h3>Error</h3>
          <pre className="traceback">{[run.error, run.traceback].filter(Boolean).join("\n\n")}</pre>
        </>
      )}
      <h3>Edge Data</h3>
      <pre>{edgeData ? JSON.stringify(edgeData, null, 2) : "Click an edge to inspect the value from this session."}</pre>
    </aside>
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

createRoot(document.getElementById("root")).render(<AuthGate />);

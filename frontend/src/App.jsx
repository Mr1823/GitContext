import { useEffect, useMemo, useRef, useState } from "react";
import { sileo } from "sileo";

const githubUrlPattern = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

async function api(route, payload) {
  const response = await fetch(route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.detail || `Request failed (${response.status})`);
  return result;
}

async function apiGet(route) {
  const response = await fetch(route, { headers: { Accept: "application/json" } });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || result.detail || `Request failed (${response.status})`);
  return result;
}

function scrollTo(element, block = "start") {
  if (!element) return;
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  element.scrollIntoView({ behavior, block });
}

function safeSvg(svgText) {
  const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const root = parsed.documentElement;
  if (root.localName.toLowerCase() !== "svg" || parsed.querySelector("parsererror")) {
    throw new Error("The diagram renderer returned invalid SVG.");
  }
  for (const element of [root, ...root.querySelectorAll("*")]) {
    if (["script", "iframe", "object", "embed"].includes(element.localName.toLowerCase())) {
      element.remove();
      continue;
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || ((name === "href" || name.endsWith(":href")) && !attribute.value.startsWith("#"))) {
        element.removeAttribute(attribute.name);
      }
    }
  }
  return new XMLSerializer().serializeToString(root);
}

function fileKind(path) {
  const extension = path.split(".").pop().toLowerCase();
  return ({ py: "PY", js: "JS", jsx: "JS", ts: "TS", tsx: "TS", java: "JV" })[extension] || "SRC";
}

function repositorySuggestions(files = []) {
  const paths = [...new Set(files)].sort();
  const groups = [
    { match: /(^|\/)(main|app|cli|server|index)\.(py|js|jsx|ts|tsx)$/i, question: (path) => `How does ${path} start or expose the application?` },
    { match: /(auth|login|session|permission|security)/i, question: (path) => `How does ${path} handle authentication or access?` },
    { match: /(route|api|web|controller|endpoint)/i, question: (path) => `How does ${path} handle repository requests?` },
    { match: /(retriev|search|embed|vector|index)/i, question: (path) => `How does ${path} find and prepare code context?` },
    { match: /(parse|chunk|diagram|ingest)/i, question: (path) => `What does ${path} extract from the repository?` },
  ];
  const suggestions = [];
  for (const group of groups) {
    const path = paths.find((candidate) => group.match.test(candidate));
    if (path) suggestions.push(group.question(path));
    if (suggestions.length === 3) break;
  }
  for (const path of paths) {
    if (suggestions.length === 3) break;
    const suggestion = `What does ${path} do in this repository?`;
    if (!suggestions.includes(suggestion)) suggestions.push(suggestion);
  }
  return suggestions;
}

function InlineText({ text }) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  const parts = [];
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > position) parts.push(text.slice(position, match.index));
    const marker = match[0];
    parts.push(marker.startsWith("`")
      ? <code key={match.index}>{marker.slice(1, -1)}</code>
      : <strong key={match.index}>{marker.slice(2, -2)}</strong>);
    position = match.index + marker.length;
  }
  if (position < text.length) parts.push(text.slice(position));
  return parts;
}

function AnswerText({ text }) {
  const blocks = [];
  let paragraph = [];
  let list = null;
  let code = null;
  let codeLanguage = "";
  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(<p key={`p-${blocks.length}`}><InlineText text={paragraph.join(" ")} /></p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const List = list.type;
    blocks.push(<List key={`list-${blocks.length}`}>{list.items.map((item, index) => <li key={index}><InlineText text={item} /></li>)}</List>);
    list = null;
  };

  for (const line of text.split(/\r?\n/)) {
    const fence = line.match(/^\s*```([\w+-]*)\s*$/);
    if (fence && code === null) {
      flushParagraph();
      flushList();
      code = [];
      codeLanguage = fence[1];
    } else if (fence && code !== null) {
      blocks.push(<pre key={`code-${blocks.length}`}><code className={codeLanguage ? `language-${codeLanguage}` : undefined}>{code.join("\n")}</code></pre>);
      code = null;
      codeLanguage = "";
    } else if (code !== null) {
      code.push(line);
    } else {
      const heading = line.match(/^#{1,3}\s+(.+)$/);
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (!line.trim()) {
        flushParagraph();
        flushList();
      } else if (heading) {
        flushParagraph();
        flushList();
        blocks.push(<h3 key={`h-${blocks.length}`}><InlineText text={heading[1]} /></h3>);
      } else if (bullet || numbered) {
        flushParagraph();
        const type = bullet ? "ul" : "ol";
        if (list?.type !== type) {
          flushList();
          list = { type, items: [] };
        }
        list.items.push((bullet || numbered)[1]);
      } else {
        flushList();
        paragraph.push(line.trim());
      }
    }
  }
  if (code !== null) blocks.push(<pre key={`code-${blocks.length}`}><code className={codeLanguage ? `language-${codeLanguage}` : undefined}>{code.join("\n")}</code></pre>);
  flushParagraph();
  flushList();
  return blocks;
}

function OrbitEmblem() {
  return (
    <svg className="orbit-emblem" viewBox="0 0 200 130" aria-hidden="true" focusable="false">
      <g fill="none" stroke="var(--border)" strokeWidth="1.5" opacity=".9">
        <ellipse cx="100" cy="65" rx="72" ry="27" transform="rotate(-31 100 65)" />
        <ellipse cx="100" cy="65" rx="72" ry="27" transform="rotate(31 100 65)" />
      </g>
      <circle cx="148" cy="22" r="4" fill="var(--accent)" />
      <circle cx="35" cy="59" r="2.7" fill="var(--accent)" opacity=".8" />
      <circle cx="112" cy="112" r="3" fill="var(--accent)" opacity=".9" />
      <g transform="translate(78 43)"><g transform="rotate(-8 22 22)">
        <rect x="4" y="4" width="36" height="36" rx="12" fill="none" stroke="var(--border)" strokeWidth="2" />
        <path d="M15 14v16M22 11v21M29 14v16" fill="none" stroke="var(--accent)" strokeWidth="4.5" strokeLinecap="round" />
      </g></g>
    </svg>
  );
}

function Message({ message, onOpenSource }) {
  const assistant = message.role === "assistant";
  return (
    <div className={`message ${message.role}`}>
      <div className="message-content">
        <div className="message-label">{assistant ? `GitContext${Number.isFinite(message.latency) ? ` · ${message.latency} ms retrieval` : ""}` : "You"}</div>
        <div className="message-text">
          {message.loading ? "Searching repository files…" : assistant ? <AnswerText text={message.text} /> : message.text}
        </div>
      </div>
      {assistant && message.sources?.length > 0 && (
        <aside className="message-citations" aria-label="Answer citations">
          {message.sources.map((source, index) => {
            const hasLine = Number.isInteger(source.start_line);
            const range = hasLine ? `:${source.start_line}${Number.isInteger(source.end_line) && source.end_line !== source.start_line ? `-${source.end_line}` : ""}` : "";
            const sourceUrl = source.source_url;
            const openUrl = (() => {
              try {
                const url = new URL(sourceUrl);
                return ["code", "documentation"].includes(source.source_type) && url.protocol === "https:" && url.hostname === "github.com" ? url.href : null;
              } catch { return null; }
            })();
            return (
              <details className="source-card" key={`${source.path}-${source.start_line}-${index}`}>
                <summary>{source.path}{range}</summary>
                <pre><code>{source.content || ""}</code></pre>
                <button className="source-open-button" type="button" onClick={() => onOpenSource(source)}>Open file in Files panel</button>
                {openUrl && <a className="citation-open-link" href={openUrl} target="_blank" rel="noopener noreferrer">Open source on GitHub</a>}
              </details>
            );
          })}
        </aside>
      )}
    </div>
  );
}

export default function App() {
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [repository, setRepository] = useState(null);
  const [repoError, setRepoError] = useState("");
  const [indexStatus, setIndexStatus] = useState("");
  const [warnings, setWarnings] = useState([]);
  const [repoBusy, setRepoBusy] = useState(false);
  const [indexBusy, setIndexBusy] = useState(false);
  const [diagramBusy, setDiagramBusy] = useState(false);
  const [questionBusy, setQuestionBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobileTab, setMobileTab] = useState("chat");
  const [panelTab, setPanelTab] = useState("files");
  const [sidePanelOpen, setSidePanelOpen] = useState(() => {
    const saved = window.localStorage.getItem("gitcontext-side-panel-open");
    return saved === null ? window.innerWidth >= 1024 : saved === "true";
  });
  const [sidePanelWidth, setSidePanelWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem("gitcontext-side-panel-width"));
    return Number.isFinite(saved) && saved >= 280 && saved <= 560 ? saved : 360;
  });
  const [isMobile, setIsMobile] = useState(() => window.matchMedia("(max-width: 600px)").matches);
  const [isTablet, setIsTablet] = useState(() => window.matchMedia("(min-width: 601px) and (max-width: 1023px)").matches);
  const [isResizingPanel, setIsResizingPanel] = useState(false);
  const [selectedFile, setSelectedFile] = useState(null);
  const [changeRepositoryOpen, setChangeRepositoryOpen] = useState(false);
  const [diagramFocused, setDiagramFocused] = useState(false);
  const [diagramZoom, setDiagramZoom] = useState(1);
  const [indexProgress, setIndexProgress] = useState({ status: "idle", stage: "Waiting to index", processed: 0, total: 0, current_file: "" });
  const [fileFilter, setFileFilter] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [diagram, setDiagram] = useState(null);
  const [diagramMarkup, setDiagramMarkup] = useState("");
  const [diagramStatus, setDiagramStatus] = useState("Build the search index to generate a diagram.");
  const [hash, setHash] = useState(window.location.hash || "#repository");
  const repoInputRef = useRef(null);
  const indexButtonRef = useRef(null);
  const panelToggleRef = useRef(null);
  const questionInputRef = useRef(null);
  const chatBodyRef = useRef(null);
  const diagramOutputRef = useRef(null);
  const workspaceRef = useRef(null);
  const mermaidRef = useRef(null);
  const diagramRenderCount = useRef(0);

  const activeRepository = repository?.repository || "";
  const indexed = Boolean(repository?.indexed_chunks);
  const visibleFiles = useMemo(() => (repository?.files || []).filter((path) => path.toLowerCase().includes(fileFilter.toLowerCase())), [repository, fileFilter]);
  const suggestedQuestions = useMemo(() => repositorySuggestions(repository?.files || []), [repository]);
  const currentWorkflowStep = !repository ? "connect" : indexBusy ? "index" : !indexed ? "index" : messages.length ? "ask" : "explore";

  useEffect(() => {
    const mobile = window.matchMedia("(max-width: 600px)");
    const tablet = window.matchMedia("(min-width: 601px) and (max-width: 1023px)");
    const change = () => { setIsMobile(mobile.matches); setIsTablet(tablet.matches); };
    mobile.addEventListener("change", change);
    tablet.addEventListener("change", change);
    return () => { mobile.removeEventListener("change", change); tablet.removeEventListener("change", change); };
  }, []);

  useEffect(() => { window.localStorage.setItem("gitcontext-side-panel-open", String(sidePanelOpen)); }, [sidePanelOpen]);
  useEffect(() => { window.localStorage.setItem("gitcontext-side-panel-width", String(sidePanelWidth)); }, [sidePanelWidth]);

  useEffect(() => {
    if (!indexBusy || !activeRepository) return undefined;
    let active = true;
    let timer;
    const poll = async () => {
      try {
        const progress = await apiGet(`/api/index/progress?repo=${encodeURIComponent(activeRepository)}`);
        if (active) setIndexProgress(progress);
      } catch { /* The index request itself reports any failure. */ }
      if (active) timer = window.setTimeout(poll, 650);
    };
    poll();
    return () => { active = false; window.clearTimeout(timer); };
  }, [indexBusy, activeRepository]);

  useEffect(() => {
    document.documentElement.classList.toggle("chat-input-focused", document.activeElement?.id === "question-input");
    const onFocus = (event) => document.documentElement.classList.toggle("chat-input-focused", event.target?.id === "question-input");
    const onBlur = (event) => { if (event.target?.id === "question-input") document.documentElement.classList.remove("chat-input-focused"); };
    document.addEventListener("focusin", onFocus);
    document.addEventListener("focusout", onBlur);
    return () => { document.removeEventListener("focusin", onFocus); document.removeEventListener("focusout", onBlur); document.documentElement.classList.remove("chat-input-focused"); };
  }, []);

  useEffect(() => {
    const onHashChange = () => setHash(window.location.hash || "#repository");
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const onPointer = (event) => {
      if (!event.target.closest("#main-navigation, #menu-toggle")) setMenuOpen(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        document.getElementById("menu-toggle")?.focus();
      }
    };
    document.addEventListener("click", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!diagramFocused) return undefined;
    const closeOnEscape = (event) => { if (event.key === "Escape") setDiagramFocused(false); };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [diagramFocused]);

  useEffect(() => {
    const svg = diagramOutputRef.current?.querySelector("svg");
    if (!svg || !diagram?.nodes) return;
    svg.querySelectorAll("g.node").forEach((element) => {
      const id = element.id.match(/flowchart-(n[0-9]+)-/)?.[1];
      const node = diagram.nodes.find((item) => item.id === id);
      if (node?.files?.length) {
        element.setAttribute("tabindex", "0");
        element.setAttribute("role", "button");
        element.setAttribute("aria-label", `Open ${node.files[0]} in Files panel`);
      }
    });
  }, [diagramMarkup, diagram]);

  useEffect(() => {
    const body = chatBodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [messages]);

  function notify(type, title, description) {
    sileo[type]({ title, description, fill: "#171b16", roundness: 12, duration: 4500 });
  }

  function resetDiagram(isReady = indexed) {
    setDiagram(null);
    setDiagramMarkup("");
    setDiagramStatus(isReady ? "Generate a diagram from this repository's current index." : "Build the search index to generate a diagram.");
  }

  function setRepositoryStatus(data) {
    const files = Number.isInteger(data.source_files) ? data.source_files : data.count;
    const chunks = Number.isInteger(data.chunks) ? data.chunks : data.indexed_chunks;
    const fields = [];
    if (Number.isInteger(files)) fields.push(`${files} files`);
    if (Number.isInteger(chunks) && chunks > 0) fields.push(`${chunks} chunks`);
    if (Number.isInteger(data.commits)) fields.push(`${data.commits} commits`);
    if (Number.isInteger(data.pull_requests)) fields.push(`${data.pull_requests} pull requests`);
    if (Number.isInteger(data.incidents)) fields.push(`${data.incidents} incidents`);
    if ((Number.isInteger(data.chunks) && data.chunks > 0) || (Number.isInteger(data.indexed_chunks) && data.indexed_chunks > 0)) {
      setIndexStatus(`Index ready${fields.length ? ` · ${fields.join(", ")}` : ""}. Ask a question or generate the architecture diagram.`);
    } else if (Number.isInteger(data.chunks)) {
      setIndexStatus("Index finished without searchable chunks. Check the indexing warnings or choose a repository with supported source files.");
    } else if (Number.isInteger(files)) {
      setIndexStatus(`Repository connected · ${files} source ${files === 1 ? "file" : "files"} found. Build the search index to enable questions and the architecture diagram.`);
    } else setIndexStatus("");
  }

  function resetWorkspace() {
    setMessages([]);
    setWarnings([]);
    setDiagram(null);
    setDiagramMarkup("");
    setDiagramStatus("Build the search index to generate a diagram.");
    setQuestion("");
    setMobileTab("chat");
    setPanelTab("files");
    setSelectedFile(null);
    setChangeRepositoryOpen(false);
  }

  async function connectRepository(event) {
    event.preventDefault();
    if (repoBusy) return;
    const path = repositoryUrl.trim();
    if (!path) return;
    const previousRepository = activeRepository;
    const previousStatus = indexStatus;
    setRepoBusy(true);
    setRepoError("");
    setIndexStatus("Checking the public GitHub repository…");
    try {
      const result = await api("/api/scan", { path });
      if (previousRepository && previousRepository !== result.repository) resetWorkspace();
      setRepository(result);
      setRepositoryStatus(result);
      setFileFilter("");
      setMobileTab("chat");
      setPanelTab("files");
      setSidePanelOpen(true);
      setSelectedFile(null);
      setChangeRepositoryOpen(false);
      notify("success", "Repository connected", `${result.count} supported source files found.`);
      scrollTo(document.getElementById("repository-panel"));
    } catch (error) {
      setRepoError(error.message);
      notify("error", "Could not connect repository", error.message);
      if (previousRepository !== path) {
        setRepository(null);
        resetWorkspace();
        setIndexStatus("");
      } else setIndexStatus(previousStatus);
    } finally {
      setRepoBusy(false);
    }
  }

  async function buildIndex() {
    if (!activeRepository || indexBusy) return;
    const previousStatus = indexStatus;
    setIndexBusy(true);
    setIndexProgress({ status: "running", stage: "Preparing repository index", processed: 0, total: repository?.files?.length || 0, current_file: "" });
    setIndexStatus("Building the search index…");
    setRepoError("");
    setWarnings([]);
    try {
      const result = await api("/api/index", { path: activeRepository });
      setRepository((current) => ({
        ...current,
        ...result,
        repository: current.repository,
        files: current.files,
        remote: current.remote,
        indexed_chunks: result.chunks,
      }));
      setRepositoryStatus(result);
      setWarnings(result.warnings || []);
      setMobileTab("chat");
      setPanelTab("files");
      setChangeRepositoryOpen(false);
      resetDiagram(result.chunks > 0);
      setIndexProgress({ status: "complete", stage: "Index complete", processed: result.source_files, total: result.source_files, current_file: "" });
      notify("success", "Search index ready", `${result.source_files} files · ${result.chunks} chunks`);
      if (result.warnings?.length) notify("warning", "Some repository context was skipped", result.warnings.join(" "));
    } catch (error) {
      setIndexProgress((current) => ({ ...current, status: "error", stage: "Indexing failed", error: error.message }));
      setRepoError(error.message);
      setIndexStatus(indexed ? "Reindexing failed. Your previous index is still available; review the error and retry." : "Indexing failed. Review the error and retry the index.");
      if (previousStatus && indexed) setIndexStatus((status) => `${status} ${previousStatus}`);
      notify("error", "Indexing failed", error.message);
    } finally {
      setIndexBusy(false);
    }
  }

  async function loadMermaid() {
    if (!mermaidRef.current) {
      mermaidRef.current = import("https://cdn.jsdelivr.net/npm/mermaid@12.2.0/dist/mermaid.esm.min.mjs")
        .then((module) => module.default)
        .catch((error) => { mermaidRef.current = null; throw error; });
    }
    return mermaidRef.current;
  }

  async function generateDiagram() {
    if (!activeRepository || !indexed || diagramBusy) return;
    setDiagramBusy(true);
    setDiagramStatus("Reading the current index and rendering its import map…");
    try {
      const result = await apiGet(`/api/diagram?repo=${encodeURIComponent(activeRepository)}`);
      if (typeof result.mermaid !== "string") throw new Error("The diagram response did not include Mermaid source.");
      setDiagram(result);
      setDiagramMarkup("");
      let renderError = null;
      try {
        const mermaid = await loadMermaid();
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          flowchart: { htmlLabels: false, useMaxWidth: false },
          themeVariables: {
            background: "#111410", primaryColor: "#171b16", primaryTextColor: "#eef1e9",
            primaryBorderColor: "#b9f279", lineColor: "#899286", secondaryColor: "#1b201a",
            tertiaryColor: "#111410", mainBkg: "#171b16", nodeBorder: "#b9f279",
            clusterBkg: "#171b16", clusterBorder: "#2b3229", titleColor: "#eef1e9",
            edgeLabelBackground: "#111410", fontFamily: "DM Sans, sans-serif", fontSize: "14px",
          },
        });
        diagramRenderCount.current += 1;
        const rendered = await mermaid.render(`gitcontext-diagram-${diagramRenderCount.current}`, result.mermaid);
        setDiagramMarkup(safeSvg(rendered.svg));
      } catch (error) {
        renderError = error;
      }
      const excluded = (result.not_analysed || []).map((item) => `${item.extension} (${item.file_count})`).join(", ");
      const status = [
        `${result.nodes?.length || 0} ${(result.nodes?.length || 0) === 1 ? "node" : "nodes"}`,
        `${result.edges?.length || 0} ${(result.edges?.length || 0) === 1 ? "import" : "imports"}`,
        typeof result.labels_status === "string" ? `Labels: ${result.labels_status}` : "",
      ].filter(Boolean);
      if (excluded) status.push(`Not analyzed: ${excluded}`);
      setDiagramStatus(renderError
        ? `Mermaid could not render this diagram. Its source is shown below. ${renderError.message || ""}`
        : status.join(" · "));
      if (renderError) {
        notify("warning", "Diagram source available", renderError.message || "Mermaid could not render the diagram.");
      } else {
        notify("success", "Architecture diagram ready", `${result.nodes?.length || 0} nodes · ${result.edges?.length || 0} connections`);
      }
    } catch (error) {
      setDiagramStatus(error.message || "Could not generate the diagram.");
      notify("error", "Could not generate diagram", error.message);
    } finally {
      setDiagramBusy(false);
    }
  }

  function diagramFileName(extension) {
    const name = activeRepository.split("/").filter(Boolean).pop() || "repository";
    const safeName = name.replace(/\.git$/i, "").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "") || "repository";
    return `${safeName}-architecture.${extension}`;
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function getExportDiagram() {
    const source = diagramOutputRef.current?.querySelector("svg");
    if (!source || !diagram?.mermaid) return null;
    const svg = source.cloneNode(true);
    const viewBox = svg.viewBox.baseVal;
    const width = viewBox.width || source.getBoundingClientRect().width;
    const height = viewBox.height || source.getBoundingClientRect().height;
    if (!width || !height) throw new Error("The diagram has no exportable size.");
    svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    svg.setAttribute("width", String(Math.ceil(width)));
    svg.setAttribute("height", String(Math.ceil(height)));
    const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    background.setAttribute("x", String(viewBox.x || 0));
    background.setAttribute("y", String(viewBox.y || 0));
    background.setAttribute("width", String(width));
    background.setAttribute("height", String(height));
    background.setAttribute("fill", "#111410");
    svg.insertBefore(background, svg.firstChild);
    return { svg, width: Math.ceil(width), height: Math.ceil(height) };
  }

  async function downloadDiagram(format) {
    try {
      if (!diagram) return;
      if (format === "mermaid") {
        downloadBlob(new Blob([diagram.mermaid], { type: "text/plain;charset=utf-8" }), diagramFileName("mmd"));
        return;
      }
      const output = getExportDiagram();
      if (!output) return;
      const content = new XMLSerializer().serializeToString(output.svg);
      if (format === "svg") {
        downloadBlob(new Blob([content], { type: "image/svg+xml;charset=utf-8" }), diagramFileName("svg"));
        return;
      }
      const url = URL.createObjectURL(new Blob([content], { type: "image/svg+xml;charset=utf-8" }));
      try {
        const image = new Image();
        image.src = url;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = output.width * 2;
        canvas.height = output.height * 2;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("This browser cannot export the diagram image.");
        if (format === "jpeg") {
          context.fillStyle = "#111410";
          context.fillRect(0, 0, canvas.width, canvas.height);
        }
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve, reject) => canvas.toBlob(
          (value) => value ? resolve(value) : reject(new Error("Could not create the diagram image.")),
          `image/${format}`,
          0.92,
        ));
        downloadBlob(blob, diagramFileName(format === "jpeg" ? "jpg" : "png"));
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (error) {
      setDiagramStatus(error.message || "Could not export the diagram.");
      notify("error", "Diagram export failed", error.message);
    }
  }

  async function copyDiagramMermaid() {
    if (!diagram?.mermaid) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(diagram.mermaid);
      else {
        const input = document.createElement("textarea");
        input.value = diagram.mermaid;
        input.setAttribute("readonly", "");
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.append(input);
        input.select();
        const copied = document.execCommand("copy");
        input.remove();
        if (!copied) throw new Error("Clipboard access is unavailable in this browser.");
      }
      setDiagramStatus("Mermaid source copied to the clipboard.");
      notify("success", "Mermaid copied", "The diagram source is ready to paste.");
    } catch (error) {
      setDiagramStatus(error.message || "Could not copy Mermaid source.");
      notify("error", "Could not copy Mermaid", error.message);
    }
  }

  async function askQuestion(value = question) {
    const cleanQuestion = value.trim();
    if (!indexed || !cleanQuestion || questionBusy) return;
    setQuestion("");
    setQuestionBusy(true);
    setMessages((current) => [...current, { role: "user", text: cleanQuestion }, { role: "assistant", loading: true }]);
    try {
      const result = await api("/api/ask", { path: activeRepository, question: cleanQuestion, top_k: 6 });
      if (typeof result.index_refreshed === "boolean") {
        setIndexStatus((current) => {
          const metrics = current.replace(/^Index ready(?: · )?/, "").replace(/\. Ask a question or generate the architecture diagram\.$/, "");
          return ["Index ready", metrics, result.index_refreshed ? "Index refreshed from GitHub" : "Index current"].filter(Boolean).join(" · ");
        });
      }
      const answer = result.answer.startsWith("Relevant code found:") && result.sources?.length
        ? "GitContext retrieved these repository excerpts. Expand a citation to read the source text."
        : result.answer;
      setMessages((current) => [...current.slice(0, -1), { role: "assistant", text: answer, sources: result.sources || [], latency: result.retrieval_ms }]);
    } catch (error) {
      setMessages((current) => [...current.slice(0, -1), { role: "assistant", text: `The question could not be answered: ${error.message}\n\nCheck the connection and try again.` }]);
      notify("error", "Question failed", error.message);
    } finally {
      setQuestionBusy(false);
      questionInputRef.current?.focus();
    }
  }

  async function openRepositoryFile(path, source = null) {
    setPanelTab("files");
    setSidePanelOpen(true);
    setSelectedFile({ path, start_line: source?.start_line, end_line: source?.end_line, loading: true, content: "" });
    if (isMobile) setMobileTab("files");
    try {
      const result = await apiGet(`/api/file?repo=${encodeURIComponent(activeRepository)}&path=${encodeURIComponent(path)}`);
      setSelectedFile({ ...result, start_line: source?.start_line, end_line: source?.end_line, loading: false });
    } catch (error) {
      setSelectedFile(null);
      notify("error", "Could not open source file", error.message);
    }
  }

  function handleDiagramNodeClick(event) {
    const nodeElement = event.target.closest?.("g.node");
    if (!nodeElement) return;
    const nodeId = nodeElement.id.match(/flowchart-(n[0-9]+)-/)?.[1];
    const node = diagram?.nodes?.find((item) => item.id === nodeId);
    if (node?.files?.[0]) openRepositoryFile(node.files[0]);
  }

  function resizeSidePanel(event) {
    if (!isResizingPanel || !workspaceRef.current) return;
    const bounds = workspaceRef.current.getBoundingClientRect();
    setSidePanelWidth(Math.max(280, Math.min(560, Math.round(bounds.right - event.clientX))));
  }

  function onWorkspaceKeyDown(event, index, tabs, selectTab, idPrefix) {
    let next = index;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[next].id);
    document.getElementById(`${idPrefix}-${tabs[next].id}`)?.focus();
  }

  function closeRepositoryPanel() {
    if (isMobile) {
      setMobileTab("chat");
      window.requestAnimationFrame(() => document.getElementById("mobile-tab-chat")?.focus());
    } else {
      setSidePanelOpen(false);
      window.requestAnimationFrame(() => panelToggleRef.current?.focus());
    }
  }

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <div className="app-shell"><div className="main-area">
        <header className="topbar">
          <a className="brand-lockup" href="/" aria-label="GitContext home">
            <svg className="brand-symbol" viewBox="0 0 44 44" aria-hidden="true" focusable="false"><g transform="rotate(-8 22 22)"><rect x="4" y="4" width="36" height="36" rx="12" fill="none" stroke="var(--border)" strokeWidth="2" /><path d="M15 14v16M22 11v21M29 14v16" fill="none" stroke="var(--accent)" strokeWidth="4.5" strokeLinecap="round" /></g></svg>
            <span className="brand-wordmark"><span>git</span><span className="brand-context">context</span></span>
          </a>
          <button className="menu-toggle" id="menu-toggle" type="button" aria-controls="main-navigation" aria-expanded={menuOpen} aria-label={menuOpen ? "Close navigation" : "Open navigation"} onClick={() => setMenuOpen((open) => !open)}><span></span><span></span><span></span></button>
          <nav className={`primary-nav${menuOpen ? " is-open" : ""}`} id="main-navigation" aria-label="Main navigation">
            {[{ href: "#repository", label: "Repository" }, { href: "#architecture", label: "Architecture", requires: "architecture" }, { href: "#repository-qa", label: "Ask", requires: "ask" }].map((item) => (
              <a key={item.label} href={item.href} data-requires-index={item.requires} aria-current={hash === item.href ? "location" : undefined} onClick={(event) => {
                if (item.requires && !activeRepository) {
                  event.preventDefault();
                  setIndexStatus("Enter a public GitHub repository URL to open this section.");
                  repoInputRef.current?.focus();
                  scrollTo(document.getElementById("repository"), "center");
                } else if (item.requires && !indexed) {
                  event.preventDefault();
                  setIndexStatus(item.requires === "architecture" ? "Build the search index before generating the architecture diagram." : "Build the search index before asking questions.");
                  indexButtonRef.current?.focus();
                  scrollTo(indexButtonRef.current, "center");
                } else if (item.requires === "architecture") {
                  setPanelTab("architecture");
                  setSidePanelOpen(true);
                  if (!diagram && indexed) generateDiagram();
                  if (isMobile) setMobileTab("diagram");
                } else if (item.requires === "ask") {
                  if (isMobile) setMobileTab("chat");
                  else questionInputRef.current?.focus();
                }
                if (isMobile) setMenuOpen(false);
              }}>{item.label}</a>
            ))}
            <a className="button button-primary header-cta" href="#repository" onClick={() => { setChangeRepositoryOpen(true); if (isMobile) setMenuOpen(false); }}>Connect repository</a>
          </nav>
        </header>

        <main id="main-content" className={`content-wrap${repository ? " has-repository" : ""}`}>
          {!repository && <section className="welcome-row">
            <div className="welcome-copy"><div className="eyebrow">CODEBASE INTELLIGENCE</div><h1>Understand an unfamiliar codebase.</h1><p className="subtitle">Inspect a public GitHub repository's architecture and source files, then ask questions with source citations.</p></div>
            <OrbitEmblem />
          </section>}

          {repository && <section className="repo-context-bar panel" aria-label="Current repository">
            <div><span className="eyebrow small-eyebrow">CURRENT REPOSITORY</span><strong>{activeRepository.split("/").filter(Boolean).pop()}</strong><span className="repo-path">{activeRepository}</span></div>
            <button className="button button-secondary" type="button" onClick={() => setChangeRepositoryOpen((open) => !open)} aria-expanded={changeRepositoryOpen}>{changeRepositoryOpen ? "Close" : "Change repository"}</button>
          </section>}

          {(!repository || changeRepositoryOpen) && <section id="repository" className="repo-card panel" aria-labelledby="connect-title">
            <div className="section-heading"><div><h2 id="connect-title">Connect a public repository</h2><p>Paste a GitHub repository URL to inspect its source files.</p></div></div>
            <form className="repo-form" onSubmit={connectRepository}>
              <label className="field-label" htmlFor="repository-path">GitHub repository URL</label>
              <div className="repo-form-row"><div className="path-input-wrap"><input ref={repoInputRef} id="repository-path" name="path" type="url" inputMode="url" aria-describedby="repository-hint" placeholder="https://github.com/owner/repository" pattern="https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" autoComplete="url" spellCheck="false" required value={repositoryUrl} onChange={(event) => { const { value } = event.target; event.target.setCustomValidity(value && !githubUrlPattern.test(value) ? "Enter a GitHub URL in this format: https://github.com/owner/repository." : ""); setRepositoryUrl(value); }} /></div>
                <button className="button button-primary" id="scan-button" type="submit" disabled={repoBusy}><span>{repoBusy ? "Checking repository…" : "Connect repository"}</span></button></div>
            </form>
            <p className="form-hint" id="repository-hint">Public GitHub repositories only. Connecting checks the repository and lists supported source files.</p>
            {indexStatus && <p className="connection-status" role="status" aria-live="polite">{indexStatus}</p>}
            {repoError && <div id="repo-error" className="notice notice-error" role="alert">{repoError}</div>}
          </section>}

          <nav className="workflow-progress" aria-label="Repository workflow">
            <ol>{[{ id: "connect", label: "Connect" }, { id: "index", label: "Index" }, { id: "explore", label: "Explore" }, { id: "ask", label: "Ask" }].map((step, index) => {
              const steps = ["connect", "index", "explore", "ask"];
              const current = steps.indexOf(currentWorkflowStep);
              return <li key={step.id} className={index < current ? "is-complete" : index === current ? "is-current" : ""} aria-current={index === current ? "step" : undefined}><span className="workflow-number">{index < current ? "✓" : index + 1}</span><span>{step.label}</span></li>;
            })}</ol>
          </nav>

          {repository && <>
            {!indexBusy && indexStatus && <p className="repository-workflow-status" role="status">{indexStatus}</p>}
            {indexBusy && <div className="index-progress panel" id="index-progress" role="status" aria-live="polite" aria-busy="true">
              <div className="index-progress-heading"><strong>{indexProgress.stage || "Preparing repository index"}</strong><span>{indexProgress.total > 0 ? `${indexProgress.processed} of ${indexProgress.total} files` : "Working through repository stages"}</span></div>
              {indexProgress.total > 0 && <progress max={indexProgress.total} value={Math.min(indexProgress.processed, indexProgress.total)} aria-label={`${indexProgress.processed} of ${indexProgress.total} files processed`} />}
              {indexProgress.current_file && <code>{indexProgress.current_file}</code>}
              <p>{indexed ? "Reindexing. Your current index remains available." : "Keep this page open while GitContext prepares the index."}</p>
            </div>}
            {repoError && <div className="notice notice-error workspace-error" role="alert">{repoError} {indexProgress.status === "error" && "Check the message and retry indexing; if reindexing failed, your previous index is unchanged."}</div>}
            {warnings.length > 0 && <div id="index-warnings" className="notice notice-warning" role="status">{warnings.join(" ")}</div>}

            <section id="repository-panel" ref={workspaceRef} className={`workspace-area${sidePanelOpen ? " panel-open" : " panel-closed"}${isTablet && sidePanelOpen ? " tablet-panel-open" : ""}`} style={{ "--side-panel-width": `${sidePanelWidth}px` }}>
              <section id="repository-qa" className="panel chat-panel" aria-labelledby="chat-title" hidden={isMobile && mobileTab !== "chat"}>
                <div className="chat-header"><div><h2 id="chat-title">Ask this repository</h2><p>Answers include source paths and line numbers.</p></div><button ref={panelToggleRef} className="panel-toggle button button-secondary" type="button" onClick={() => setSidePanelOpen((open) => !open)} aria-expanded={sidePanelOpen} aria-controls={sidePanelOpen ? "repository-side-panel" : undefined}>{sidePanelOpen ? "Hide panel" : "Files and diagram"}</button></div>
                <div className="chat-body" id="chat-body" ref={chatBodyRef} role="log" aria-live="polite" aria-relevant="additions text">
                  {!indexed ? <div className="workspace-empty" role="status"><h3>{repository.count ? "Index this repository" : "No supported source files found"}</h3><p>{repository.count ? "Build the search index to explore files, view the architecture diagram, and ask questions." : "Connect a repository with supported source files to continue."}</p><button className="button button-primary" type="button" onClick={() => repository.count ? buildIndex() : setChangeRepositoryOpen(true)} disabled={!repository.count || indexBusy}>{indexBusy ? "Indexing…" : "Build search index"}</button></div> : messages.length === 0 ? <div className="empty-chat" id="empty-chat"><h3>Ask your first question</h3><p>Choose a question based on files in this repository, or ask in your own words.</p>{suggestedQuestions.length > 0 && <div className="suggestion-list">{suggestedQuestions.map((item) => <button key={item} className="suggestion" type="button" disabled={questionBusy || indexBusy} onClick={() => askQuestion(item)}>{item}</button>)}</div>}</div> : messages.map((message, index) => <Message key={`message-${index}`} message={message} onOpenSource={(source) => openRepositoryFile(source.path, source)} />)}
                </div>
                <form className="question-form" onSubmit={(event) => { event.preventDefault(); askQuestion(); }}><label className="sr-only" htmlFor="question-input">Ask a question about this repository</label><textarea ref={questionInputRef} id="question-input" name="question" rows="1" maxLength="4000" placeholder={indexed ? "Ask about this repository's code" : "Build the index to ask about this repository"} required disabled={!indexed || questionBusy || indexBusy} value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); askQuestion(); } }} onInput={(event) => { event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 130)}px`; }} /><button id="ask-button" className="send-button" type="submit" aria-label="Send question" disabled={!indexed || questionBusy || indexBusy} aria-busy={questionBusy}>{questionBusy ? "…" : "Ask"}</button><div className="input-meta"><span>Enter to send</span></div></form>
              </section>

              {sidePanelOpen && <>
                {isTablet && <button className="side-panel-backdrop" type="button" aria-label="Close repository panel" onClick={() => setSidePanelOpen(false)} />}
                {!isMobile && !isTablet && <button className="panel-resizer" type="button" role="separator" aria-label="Resize repository side panel" aria-orientation="vertical" aria-valuemin="280" aria-valuemax="560" aria-valuenow={sidePanelWidth} onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); setIsResizingPanel(true); }} onPointerMove={resizeSidePanel} onPointerUp={() => setIsResizingPanel(false)} onPointerCancel={() => setIsResizingPanel(false)} onKeyDown={resizePanelKeyDown} />}
                <aside id="repository-side-panel" className={`repository-side-panel panel${diagramFocused && panelTab === "architecture" ? " is-diagram-focused" : ""}`} aria-label="Repository files and architecture" hidden={isMobile && mobileTab === "chat"}>
                  <div className="side-panel-header">
                    <div className="side-panel-tabs" role="tablist" aria-label="Repository side panel">
                      {[{ id: "files", label: "Files" }, { id: "architecture", label: "Diagram" }].map((tab, index, tabs) => <button key={tab.id} id={`side-tab-${tab.id}`} type="button" role="tab" aria-controls={`side-panel-${tab.id}`} aria-selected={panelTab === tab.id} tabIndex={panelTab === tab.id ? 0 : -1} onClick={() => { setPanelTab(tab.id); setSelectedFile(null); if (tab.id === "architecture") { setDiagramFocused(false); if (!diagram && indexed) generateDiagram(); } }} onKeyDown={(event) => onWorkspaceKeyDown(event, index, tabs, setPanelTab, "side-tab")}>{tab.label}</button>)}
                    </div>
                    {(isTablet || isMobile) && <button className="button button-secondary side-close" type="button" onClick={closeRepositoryPanel} aria-label="Close repository panel">Close</button>}
                  </div>

                  <div id="side-panel-files" className="side-panel-content" role="tabpanel" aria-labelledby="side-tab-files" tabIndex="0" hidden={panelTab !== "files"}>
                    {selectedFile ? <div className="file-preview">
                      <div className="file-preview-heading"><button className="text-action" type="button" onClick={() => setSelectedFile(null)}>Back to files</button><h2>{selectedFile.path}</h2></div>
                      {selectedFile.loading ? <p role="status">Loading file…</p> : <pre className="file-code" aria-label={`Source for ${selectedFile.path}`} tabIndex="0">{selectedFile.content.split("\n").map((line, index) => { const lineNumber = index + 1; const highlighted = lineNumber >= (selectedFile.start_line || 0) && lineNumber <= (selectedFile.end_line || 0); return <span className={highlighted ? "source-line is-cited" : "source-line"} key={lineNumber}><span className="line-number">{lineNumber}</span><code>{line || " "}</code></span>; })}</pre>}
                    </div> : <div className="side-files-view">
                      <div className="files-heading"><div><h2>Source files</h2><p>{repository.files?.length || 0} supported source files</p></div><span className="file-count-pill">{repository.files?.length || 0}</span></div>
                      <label className="file-search"><span className="sr-only">Filter repository files</span><input id="file-filter" type="search" placeholder="Filter repository files" aria-label="Filter repository files" value={fileFilter} onChange={(event) => setFileFilter(event.target.value)} /></label>
                      <div id="file-list" className="file-list">{visibleFiles.length ? visibleFiles.map((path) => <button className="file-row file-row-button" type="button" key={path} onClick={() => openRepositoryFile(path)}><span className="file-type">{fileKind(path)}</span><span title={path}>{path}</span></button>) : <div className="no-files">{repository.files?.length ? "No files match this filter." : "No supported source files found in this repository."}</div>}</div>
                    </div>}
                  </div>

                  <div id="side-panel-architecture" className="side-panel-content side-diagram-view" role="tabpanel" aria-labelledby="side-tab-architecture" tabIndex="0" hidden={panelTab !== "architecture"}>
                    <div className="diagram-heading"><div><div className="eyebrow small-eyebrow">REPOSITORY MAP</div><h2 id="diagram-title">Architecture diagram</h2><p>Import relationships from the current index.</p></div><div className="diagram-controls">
                      <button className="button button-primary" id="diagram-button" type="button" disabled={!indexed || diagramBusy} aria-busy={diagramBusy} onClick={generateDiagram}><span>{diagramBusy ? "Generating…" : diagram ? "Refresh diagram" : "Generate diagram"}</span></button>
                      {diagram && <div className="diagram-downloads" id="diagram-downloads" role="group" aria-label="Download architecture diagram"><button className="button button-secondary" type="button" onClick={() => downloadDiagram("png")}>Download PNG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("jpeg")}>Download JPEG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("svg")}>Download SVG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("mermaid")}>Download Mermaid</button><button className="button button-secondary" type="button" onClick={copyDiagramMermaid}>Copy Mermaid</button></div>}
                    </div></div>
                    <div className="diagram-view-tools"><span id="diagram-status" className="diagram-status" aria-live="polite">{diagramStatus}</span><div className="diagram-zoom-controls" role="group" aria-label="Diagram view controls"><button className="button button-secondary" type="button" onClick={() => setDiagramZoom((zoom) => Math.max(.5, Math.round((zoom - .1) * 10) / 10))} aria-label="Zoom out" disabled={diagramZoom <= .5}>−</button><span>{Math.round(diagramZoom * 100)}%</span><button className="button button-secondary" type="button" onClick={() => setDiagramZoom((zoom) => Math.min(2, Math.round((zoom + .1) * 10) / 10))} aria-label="Zoom in" disabled={diagramZoom >= 2}>+</button><button className="button button-secondary" type="button" onClick={() => setDiagramZoom(1)}>Reset zoom</button><button className="button button-secondary" type="button" onClick={() => setDiagramFocused((focused) => !focused)} aria-expanded={diagramFocused}>{diagramFocused ? "Exit focus" : "Focus diagram"}</button></div></div>
                    <div id="diagram-output" ref={diagramOutputRef} className={`diagram-output${diagramMarkup || diagram?.mermaid ? "" : " is-empty"}`} role="region" aria-label="Repository architecture diagram; scroll or pan to inspect" tabIndex="0" onKeyDown={(event) => { if ((event.key === "Enter" || event.key === " ") && event.target.closest?.("g.node")) { event.preventDefault(); handleDiagramNodeClick(event); } }}>
                      {!diagram && <div className="diagram-empty"><p>{indexed ? "Generate a diagram to inspect repository structure." : "Build the search index before generating a diagram."}</p></div>}
                      {diagramMarkup ? <div className="diagram-zoom-surface" onClick={handleDiagramNodeClick} style={{ width: `${diagramZoom * 100}%` }} dangerouslySetInnerHTML={{ __html: diagramMarkup }} /> : diagram?.mermaid && <pre className="diagram-source-fallback" role="alert"><code>{diagram.mermaid}</code></pre>}
                    </div>
                    {diagram?.citations?.length > 0 && <details id="diagram-sources" className="diagram-sources"><summary>Source references</summary><ul>{diagram.citations.map((citation, index) => <li key={`${citation.path}-${citation.start_line}-${index}`}><code>{citation.start_line ? `${citation.path}:${citation.start_line}` : citation.path}</code></li>)}</ul></details>}
                  </div>
                </aside>
              </>}
            </section>
            <nav className="mobile-workspace-tabs" role="tablist" aria-label="Repository views">
              {[{ id: "chat", label: "Chat" }, { id: "files", label: "Files" }, { id: "diagram", label: "Diagram" }].map((tab, index, tabs) => <button key={tab.id} id={`mobile-tab-${tab.id}`} type="button" role="tab" aria-controls={tab.id === "chat" ? "repository-qa" : "repository-side-panel"} aria-selected={mobileTab === tab.id} tabIndex={mobileTab === tab.id ? 0 : -1} onClick={() => { setMobileTab(tab.id); if (tab.id !== "chat") { setPanelTab(tab.id === "diagram" ? "architecture" : "files"); setSidePanelOpen(true); if (tab.id === "diagram" && !diagram) generateDiagram(); } }} onKeyDown={(event) => onWorkspaceKeyDown(event, index, tabs, setMobileTab, "mobile-tab")}>{tab.label}</button>)}
            </nav>
          </>}
        </main>
        <footer className="footer"><a className="footer-brand" href="/" aria-label="GitContext home">GitContext</a><a className="footer-link" href="https://github.com/Mr1823/GitContext">GitHub repository</a></footer>
      </div></div>
    </>
  );
}

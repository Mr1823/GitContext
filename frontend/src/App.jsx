import { useEffect, useMemo, useRef, useState } from "react";
import { sileo } from "sileo";

const githubUrlPattern = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const workspaceViews = ["files", "architecture", "ask"];

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

function Message({ message }) {
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
  const [activeTab, setActiveTab] = useState("files");
  const [isMobile, setIsMobile] = useState(() => window.matchMedia("(max-width: 780px)").matches);
  const [fileFilter, setFileFilter] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [diagram, setDiagram] = useState(null);
  const [diagramMarkup, setDiagramMarkup] = useState("");
  const [diagramStatus, setDiagramStatus] = useState("Build the search index to generate a diagram.");
  const [hash, setHash] = useState(window.location.hash || "#repository");
  const repoInputRef = useRef(null);
  const indexButtonRef = useRef(null);
  const questionInputRef = useRef(null);
  const chatBodyRef = useRef(null);
  const diagramOutputRef = useRef(null);
  const mermaidRef = useRef(null);
  const diagramRenderCount = useRef(0);

  const activeRepository = repository?.repository || "";
  const indexed = Boolean(repository?.indexed_chunks);
  const visibleFiles = useMemo(() => (repository?.files || []).filter((path) => path.toLowerCase().includes(fileFilter.toLowerCase())), [repository, fileFilter]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 780px)");
    const change = () => setIsMobile(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
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
    setActiveTab("files");
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
      setActiveTab("files");
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
      setActiveTab("files");
      resetDiagram(result.chunks > 0);
      notify("success", "Search index ready", `${result.source_files} files · ${result.chunks} chunks`);
      if (result.warnings?.length) notify("warning", "Some repository context was skipped", result.warnings.join(" "));
    } catch (error) {
      setRepoError(error.message);
      setIndexStatus(indexed ? "Index update failed. The current index is still available." : "Indexing failed. Resolve the error above, then try again.");
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

  function onWorkspaceKeyDown(event, index) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % workspaceViews.length;
    else if (event.key === "ArrowLeft") next = (index + workspaceViews.length - 1) % workspaceViews.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = workspaceViews.length - 1;
    else return;
    event.preventDefault();
    const view = workspaceViews[next];
    setActiveTab(view);
    document.getElementById(`workspace-tab-${view}`)?.focus();
  }

  const emptyChatText = indexed
    ? "Choose an example or ask about a file, function, or request flow."
    : "Build the search index to enable questions. Answers include source paths and line numbers.";

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
                } else if (item.requires) setActiveTab(item.requires);
                if (isMobile) setMenuOpen(false);
              }}>{item.label}</a>
            ))}
            <a className="button button-primary header-cta" href="#repository" onClick={() => isMobile && setMenuOpen(false)}>Connect repository</a>
          </nav>
        </header>

        <main id="main-content" className="content-wrap">
          <section className="welcome-row">
            <div className="welcome-copy"><div className="eyebrow">CODEBASE INTELLIGENCE</div><h1>Understand an unfamiliar codebase.</h1><p className="subtitle">Inspect a public GitHub repository's architecture and source files, then ask questions with source citations.</p><p className={`index-status${indexStatus ? "" : " hidden"}`} aria-live="polite">{indexStatus}</p></div>
            <OrbitEmblem />
          </section>

          <section id="repository" className="repo-card panel" aria-labelledby="connect-title">
            <div className="section-heading"><div><h2 id="connect-title">Connect a public repository</h2><p>Paste a GitHub repository URL to inspect its source files.</p></div></div>
            <form className="repo-form" onSubmit={connectRepository}>
              <label className="field-label" htmlFor="repository-path">GitHub repository URL</label>
              <div className="repo-form-row"><div className="path-input-wrap"><input ref={repoInputRef} id="repository-path" name="path" type="url" inputMode="url" aria-describedby="repository-hint" placeholder="https://github.com/owner/repository" pattern="https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" autoComplete="url" spellCheck="false" required value={repositoryUrl} onChange={(event) => { const { value } = event.target; event.target.setCustomValidity(value && !githubUrlPattern.test(value) ? "Enter a GitHub URL in this format: https://github.com/owner/repository." : ""); setRepositoryUrl(value); }} /></div>
                <button className="button button-primary" id="scan-button" type="submit" disabled={repoBusy}><span>{repoBusy ? "Checking repository…" : "Connect repository"}</span></button></div>
            </form>
            <p className="form-hint" id="repository-hint">Public GitHub repositories only. Connecting checks the repository and lists supported source files.</p>
            {repoError && <div id="repo-error" className="notice notice-error" role="alert">{repoError}</div>}
          </section>

          {repository && <section id="repository-panel" className="workspace-grid">
            <div className="workspace-tabs" role="tablist" aria-label="Repository workspace">
              {[{ id: "files", label: "Files" }, { id: "architecture", label: "Architecture" }, { id: "ask", label: "Ask" }].map((tab, index) => <button key={tab.id} id={`workspace-tab-${tab.id}`} type="button" role="tab" aria-controls={tab.id === "files" ? "workspace-panel-files" : tab.id === "ask" ? "repository-qa" : "architecture"} aria-selected={activeTab === tab.id} data-workspace-tab={tab.id} tabIndex={activeTab === tab.id ? 0 : -1} onClick={() => setActiveTab(tab.id)} onKeyDown={(event) => onWorkspaceKeyDown(event, index)}>{tab.label}</button>)}
            </div>

            <section id="architecture" className="panel diagram-panel" role="tabpanel" aria-labelledby="workspace-tab-architecture" tabIndex="0" hidden={isMobile && activeTab !== "architecture"}>
              <div className="diagram-heading"><div><div className="eyebrow small-eyebrow">REPOSITORY MAP</div><h2 id="diagram-title">Architecture diagram</h2><p>Import relationships from the current index.</p></div><div className="diagram-controls">
                <button className="button button-primary" id="diagram-button" type="button" disabled={!indexed || diagramBusy} aria-busy={diagramBusy} onClick={generateDiagram}><span>{diagramBusy ? "Generating…" : diagram ? "Refresh diagram" : "Generate diagram"}</span></button>
                {diagram && <div className="diagram-downloads" id="diagram-downloads" role="group" aria-label="Download architecture diagram"><button className="button button-secondary" type="button" onClick={() => downloadDiagram("png")}>Download PNG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("jpeg")}>Download JPEG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("svg")}>Download SVG</button><button className="button button-secondary" type="button" onClick={() => downloadDiagram("mermaid")}>Download Mermaid</button><button className="button button-secondary" type="button" onClick={copyDiagramMermaid}>Copy Mermaid</button></div>}
              </div></div>
              <p id="diagram-status" className="diagram-status" aria-live="polite">{diagramStatus}</p>
              <div id="diagram-output" ref={diagramOutputRef} className={`diagram-output${diagramMarkup || diagram?.mermaid ? "" : " hidden"}`} role="region" aria-label="Repository architecture diagram" tabIndex="0">
                {diagramMarkup ? <div dangerouslySetInnerHTML={{ __html: diagramMarkup }} /> : diagram?.mermaid && <pre className="diagram-source-fallback" role="alert"><code>{diagram.mermaid}</code></pre>}
              </div>
              {diagram?.citations?.length > 0 && <details id="diagram-sources" className="diagram-sources"><summary>Source references</summary><ul>{diagram.citations.map((citation, index) => <li key={`${citation.path}-${citation.start_line}-${index}`}><code>{citation.start_line ? `${citation.path}:${citation.start_line}` : citation.path}</code></li>)}</ul></details>}
            </section>

            <div className="left-column" id="workspace-panel-files" role="tabpanel" aria-labelledby="workspace-tab-files" tabIndex="0" hidden={isMobile && activeTab !== "files"}>
              <section className="panel repo-summary"><div className="panel-header"><div><div className="eyebrow small-eyebrow">CURRENT REPOSITORY</div><h2 id="repo-name">{activeRepository.split(/[\\/]/).filter(Boolean).pop() || activeRepository}</h2></div><span className="git-badge" id="git-badge">{repository.remote ? "GitHub repository" : ""}</span></div>
                <div className="repo-path" id="repo-full-path">{activeRepository}</div><div className="stats-row"><div><strong id="file-count">{repository.files?.length || 0}</strong><span>source files</span></div><div><strong id="chunk-count">{repository.indexed_chunks || 0}</strong><span>indexed chunks</span></div></div>
                <button className="button button-secondary full-width" id="index-button" ref={indexButtonRef} type="button" onClick={buildIndex} disabled={indexBusy} aria-busy={indexBusy}><span>{indexBusy ? "Indexing…" : indexed ? "Rebuild search index" : "Build search index"}</span></button>
                {indexBusy && <div id="index-progress" className="progress-wrap" role="status" aria-live="polite" aria-busy="true"><div className="progress-track" aria-hidden="true"><span></span></div><span>Reading repository files and building the index…</span></div>}
                {warnings.length > 0 && <div id="index-warnings" className="notice notice-warning" role="status">{warnings.join(" ")}</div>}
              </section>

              <section className="panel files-panel"><div className="files-heading"><div><h2>Source files</h2><p id="file-subtitle">{repository.files?.length || 0} supported source {(repository.files?.length || 0) === 1 ? "file" : "files"}</p></div><span className="file-count-pill" id="file-pill">{repository.files?.length || 0}</span></div>
                <div className="file-search"><input id="file-filter" type="search" placeholder="Filter repository files" aria-label="Filter repository files" value={fileFilter} onChange={(event) => setFileFilter(event.target.value)} /></div>
                <div id="file-list" className="file-list">{visibleFiles.length ? visibleFiles.map((path) => <div className="file-row" key={path}><span className="file-type">{fileKind(path)}</span><span title={path}>{path}</span></div>) : <div className="no-files">{repository.files?.length ? "No files match this filter." : "No supported source files found in this repository."}</div>}</div>
              </section>
            </div>

            <section id="repository-qa" className="panel chat-panel" role="tabpanel" aria-labelledby="workspace-tab-ask" tabIndex="0" hidden={isMobile && activeTab !== "ask"}>
              <div className="chat-header"><div><h2>Ask this repository</h2><p>Answers include source paths and line numbers.</p></div></div>
              <div className="chat-body" id="chat-body" ref={chatBodyRef} role="log" aria-live="polite" aria-relevant="additions text">
                {messages.length === 0 ? <div className="empty-chat" id="empty-chat"><h3>Ask about this codebase</h3><p>{emptyChatText}</p><div className="suggestion-list">{["Where is the application entry point?", "How does authentication work?", "What are the main modules?"].map((item) => <button key={item} className="suggestion" type="button" data-question={item} disabled={!indexed || questionBusy} onClick={() => askQuestion(item)}>{item}</button>)}</div></div> : messages.map((message, index) => <Message key={`message-${index}`} message={message} />)}
              </div>
              <form className="question-form" onSubmit={(event) => { event.preventDefault(); askQuestion(); }}><label className="sr-only" htmlFor="question-input">Ask a question about this repository</label><textarea ref={questionInputRef} id="question-input" name="question" rows="1" maxLength="4000" placeholder="Ask about this repository's code" required disabled={!indexed || questionBusy} value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); askQuestion(); } }} onInput={(event) => { event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 130)}px`; }} /><button id="ask-button" className="send-button" type="submit" aria-label="Send question" disabled={!indexed || questionBusy} aria-busy={questionBusy}>{questionBusy ? "…" : "Ask"}</button><div className="input-meta"><span>Enter to send</span></div></form>
            </section>
          </section>}
        </main>
        <footer className="footer"><a className="footer-brand" href="/" aria-label="GitContext home">GitContext</a><a className="footer-link" href="https://github.com/Mr1823/GitContext">GitHub repository</a></footer>
      </div></div>
    </>
  );
}

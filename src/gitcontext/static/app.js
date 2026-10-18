const byId = (id) => document.getElementById(id);
const repoForm = byId("repository-form");
const repoInput = byId("repository-path");
const repoPanel = byId("repository-panel");
const repoError = byId("repo-error");
const fileList = byId("file-list");
const questionInput = byId("question-input");
const chatBody = byId("chat-body");
const githubUrlPattern = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
let activeRepository = "";
let indexed = false;
let mermaidPromise;
let diagramRenderNumber = 0;

const storedPath = sessionStorage.getItem("gitcontext.repository");
if (storedPath && githubUrlPattern.test(storedPath)) repoInput.value = storedPath;

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

function updateDiagramAvailability() {
  const button = byId("diagram-button");
  const output = byId("diagram-output");
  const status = byId("diagram-status");
  button.disabled = !indexed;
  button.querySelector("span").textContent = output.hasChildNodes()
    ? "Refresh diagram"
    : "Generate diagram";
  if (!indexed) status.textContent = "Build the search index to generate a diagram.";
  else if (!output.hasChildNodes() && status.textContent === "Build the search index to generate a diagram.") {
    status.textContent = "Generate a diagram from this repository's current index.";
  }
}

function resetDiagram() {
  byId("diagram-output").replaceChildren();
  byId("diagram-output").classList.add("hidden");
  byId("diagram-sources").classList.add("hidden");
  byId("diagram-source-list").replaceChildren();
  byId("diagram-status").textContent = "Build the search index to generate a diagram.";
  updateDiagramAvailability();
}

function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import("https://cdn.jsdelivr.net/npm/mermaid@12.2.0/dist/mermaid.esm.min.mjs")
      .then((module) => module.default)
      .catch((error) => {
        mermaidPromise = undefined;
        throw error;
      });
  }
  return mermaidPromise;
}

function safeSvg(svgText) {
  const document = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const root = document.documentElement;
  if (root.localName.toLowerCase() !== "svg" || document.querySelector("parsererror")) {
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
  return document.importNode(root, true);
}

function renderDiagramSources(citations) {
  const list = byId("diagram-source-list");
  list.replaceChildren();
  for (const citation of citations) {
    if (typeof citation.path !== "string") continue;
    const item = document.createElement("li");
    const label = document.createElement("code");
    label.textContent = citation.start_line
      ? `${citation.path}:${citation.start_line}`
      : citation.path;
    item.append(label);
    list.append(item);
  }
  byId("diagram-sources").classList.toggle("hidden", list.childElementCount === 0);
}

async function generateDiagram() {
  if (!activeRepository || !indexed) return;
  const button = byId("diagram-button");
  setBusy(button, true, "Generating…");
  byId("diagram-status").textContent = "Reading the current index and rendering its import map…";
  try {
    const result = await apiGet(`/api/diagram?repo=${encodeURIComponent(activeRepository)}`);
    if (typeof result.mermaid !== "string") throw new Error("The diagram response did not include Mermaid source.");
    const mermaid = await loadMermaid();
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      flowchart: { htmlLabels: false, useMaxWidth: false },
      themeVariables: {
        background: "#111410",
        primaryColor: "#171b16",
        primaryTextColor: "#eef1e9",
        primaryBorderColor: "#b9f279",
        lineColor: "#899286",
        secondaryColor: "#1b201a",
        tertiaryColor: "#111410",
        mainBkg: "#171b16",
        nodeBorder: "#b9f279",
        clusterBkg: "#171b16",
        clusterBorder: "#2b3229",
        titleColor: "#eef1e9",
        edgeLabelBackground: "#111410",
        fontFamily: "DM Sans, sans-serif",
        fontSize: "14px",
      },
    });
    diagramRenderNumber += 1;
    const rendered = await mermaid.render(`gitcontext-diagram-${diagramRenderNumber}`, result.mermaid);
    const output = byId("diagram-output");
    output.replaceChildren(safeSvg(rendered.svg));
    output.classList.remove("hidden");
    renderDiagramSources(Array.isArray(result.citations) ? result.citations : []);
    const nodeCount = Array.isArray(result.nodes) ? result.nodes.length : 0;
    const edgeCount = Array.isArray(result.edges) ? result.edges.length : 0;
    const notAnalysed = Array.isArray(result.not_analysed) ? result.not_analysed : [];
    const excluded = notAnalysed.map((item) => `${item.extension} (${item.file_count})`).join(", ");
    const status = [`${nodeCount} ${nodeCount === 1 ? "node" : "nodes"}`, `${edgeCount} ${edgeCount === 1 ? "import" : "imports"}`, typeof result.labels_status === "string" ? `Labels: ${result.labels_status}` : ""];
    if (excluded) status.push(`Not analyzed: ${excluded}`);
    byId("diagram-status").textContent = status.filter(Boolean).join(" · ");
  } catch (error) {
    byId("diagram-status").textContent = error.message || "Could not generate the diagram.";
  } finally {
    updateDiagramAvailability();
  }
}

function setNotice(message) {
  repoError.textContent = message;
  repoError.classList.toggle("hidden", !message);
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  const span = button.querySelector("span:first-child");
  if (span && label) span.textContent = label;
}

function fileKind(path) {
  const extension = path.split(".").pop().toLowerCase();
  return ({ py: "PY", js: "JS", jsx: "JS", ts: "TS", tsx: "TS", java: "JV" })[extension] || "SRC";
}

function renderFiles(files) {
  fileList.replaceChildren();
  byId("file-count").textContent = String(files.length);
  byId("file-pill").textContent = String(files.length);
  byId("file-subtitle").textContent = `${files.length} supported source ${files.length === 1 ? "file" : "files"}`;
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "no-files";
    empty.textContent = "No supported source files found in this repository.";
    fileList.append(empty);
    return;
  }
  for (const path of files) {
    const row = document.createElement("div");
    row.className = "file-row";
    const kind = document.createElement("span");
    kind.className = "file-type";
    kind.textContent = fileKind(path);
    const name = document.createElement("span");
    name.textContent = path;
    name.title = path;
    row.append(kind, name);
    fileList.append(row);
  }
}

function setIndexStatus(data) {
  const files = Number.isInteger(data.source_files) ? data.source_files : data.count;
  const chunks = Number.isInteger(data.chunks) ? data.chunks : data.indexed_chunks;
  const commits = data.commits;
  const pullRequests = data.pull_requests;
  const incidents = data.incidents;
  const hasIndex = (Number.isInteger(data.chunks) && data.chunks > 0)
    || (Number.isInteger(data.indexed_chunks) && data.indexed_chunks > 0);
  const fields = [];
  if (Number.isInteger(files)) fields.push(`${files} files`);
  if (Number.isInteger(chunks) && chunks > 0) fields.push(`${chunks} chunks`);
  if (Number.isInteger(commits)) fields.push(`${commits} commits`);
  if (Number.isInteger(pullRequests)) fields.push(`${pullRequests} pull requests`);
  if (Number.isInteger(incidents)) fields.push(`${incidents} incidents`);
  const line = byId("index-status");
  line.textContent = hasIndex && fields.length ? `Index: ${fields.join(", ")}` : "";
  line.classList.toggle("hidden", !line.textContent);
}

function showRepository(data) {
  const repositoryChanged = activeRepository && activeRepository !== data.repository;
  if (repositoryChanged) {
    chatBody.replaceChildren(byId("empty-chat"));
    byId("empty-chat").classList.remove("hidden");
    resetDiagram();
  }
  activeRepository = data.repository;
  indexed = Boolean(data.indexed_chunks);
  sessionStorage.setItem("gitcontext.repository", activeRepository);
  byId("repo-name").textContent = activeRepository.split(/[\\/]/).filter(Boolean).pop() || activeRepository;
  byId("repo-full-path").textContent = activeRepository;
  byId("git-badge").textContent = data.remote ? "GitHub repository" : "";
  byId("chunk-count").textContent = String(data.indexed_chunks);
  setIndexStatus(data);
  questionInput.disabled = !indexed;
  byId("ask-button").disabled = !indexed;
  updateDiagramAvailability();
  byId("index-button").disabled = false;
  byId("index-button").querySelector("span").textContent = indexed ? "Rebuild search index" : "Build search index";
  byId("empty-chat").classList.toggle("hidden", indexed);
  renderFiles(data.files);
  repoPanel.classList.remove("hidden");
}

repoForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setNotice("");
  const path = repoInput.value.trim();
  if (!path) return;
  const button = byId("scan-button");
  setBusy(button, true, "Checking repository…");
  try {
    const result = await api("/api/scan", { path });
    showRepository(result);
    repoPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setNotice(error.message);
    repoPanel.classList.add("hidden");
    setIndexStatus({});
  } finally {
    setBusy(button, false, "Index repository");
  }
});

byId("index-button").addEventListener("click", async () => {
  if (!activeRepository) return;
  const button = byId("index-button");
  setBusy(button, true, "Indexing…");
  byId("index-progress").classList.remove("hidden");
  setNotice("");
  try {
    const result = await api("/api/index", { path: activeRepository });
    indexed = result.chunks > 0;
    byId("chunk-count").textContent = String(result.chunks);
    questionInput.disabled = !indexed;
    byId("ask-button").disabled = !indexed;
    updateDiagramAvailability();
    byId("empty-chat").classList.toggle("hidden", indexed);
    setIndexStatus(result);
    button.querySelector("span").textContent = indexed ? "Rebuild search index" : "Build search index";
    const warnings = result.warnings || [];
    const warningBox = byId("index-warnings");
    warningBox.textContent = warnings.join(" ");
    warningBox.classList.toggle("hidden", warnings.length === 0);
  } catch (error) {
    setNotice(error.message);
  } finally {
    byId("index-progress").classList.add("hidden");
    button.disabled = false;
    button.querySelector("span").textContent = indexed ? "Rebuild search index" : "Build search index";
  }
});

byId("diagram-button").addEventListener("click", generateDiagram);

byId("file-filter").addEventListener("input", (event) => {
  const filter = event.target.value.toLowerCase();
  for (const row of fileList.querySelectorAll(".file-row")) {
    row.classList.toggle("hidden", !row.lastElementChild.textContent.toLowerCase().includes(filter));
  }
});

function appendMessage(role, text, sources = [], latency = null) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  const content = document.createElement("div");
  content.className = "message-content";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : `GitContext${latency === null ? "" : ` · ${latency} ms retrieval`}`;
  const body = document.createElement("div");
  body.className = "message-text";
  body.textContent = text;
  content.append(label, body);
  wrapper.append(content);
  if (sources.length) {
    const citations = document.createElement("aside");
    citations.className = "message-citations";
    citations.setAttribute("aria-label", "Answer citations");
    for (const source of sources) {
      const citation = document.createElement("details");
      citation.className = "source-card";
      const summary = document.createElement("summary");
      summary.textContent = `${source.path}:${source.start_line}`;
      const code = document.createElement("pre");
      code.textContent = source.content;
      citation.append(summary, code);
      citations.append(citation);
    }
    wrapper.append(citations);
  }
  chatBody.append(wrapper);
  chatBody.scrollTop = chatBody.scrollHeight;
}

async function askQuestion(question) {
  if (!indexed || !question.trim()) return;
  byId("empty-chat").classList.add("hidden");
  appendMessage("user", question.trim());
  questionInput.value = "";
  const loading = document.createElement("div");
  loading.className = "message assistant";
  loading.innerHTML = '<div class="message-content"><div class="message-label">Searching repository files</div></div>';
  chatBody.append(loading);
  chatBody.scrollTop = chatBody.scrollHeight;
  byId("ask-button").disabled = true;
  try {
    const result = await api("/api/ask", { path: activeRepository, question: question.trim(), top_k: 6 });
    loading.remove();
    if (typeof result.index_refreshed === "boolean") {
      const status = byId("index-status");
      const counts = result.index_refreshed ? "" : status.textContent.split(" · ")[0];
      const freshness = result.index_refreshed ? "Index refreshed from GitHub" : "Index current";
      status.textContent = [counts, freshness].filter(Boolean).join(" · ");
      status.classList.remove("hidden");
    }
    const answer = result.answer.startsWith("Relevant code found:") && result.sources.length
      ? "GitContext retrieved these repository excerpts. Expand a citation to read the source text."
      : result.answer;
    appendMessage("assistant", answer, result.sources, result.retrieval_ms);
  } catch (error) {
    loading.remove();
    appendMessage("assistant", error.message);
  } finally {
    byId("ask-button").disabled = !indexed;
    questionInput.focus();
  }
}

byId("question-form").addEventListener("submit", (event) => {
  event.preventDefault();
  askQuestion(questionInput.value);
});

questionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    askQuestion(questionInput.value);
  }
  questionInput.style.height = "auto";
  questionInput.style.height = `${Math.min(questionInput.scrollHeight, 130)}px`;
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => askQuestion(button.dataset.question));
});

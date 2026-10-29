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
let currentDiagramSource = "";
let mobileWorkspaceView = "files";
const mobileWorkspace = window.matchMedia("(max-width: 780px)");
const workspacePanels = {
  files: byId("workspace-panel-files"),
  architecture: byId("architecture"),
  ask: byId("repository-qa"),
};
const workspaceTabs = [...document.querySelectorAll("[data-workspace-tab]")];

function setMobileWorkspaceView(view, focusTab = false) {
  if (!workspacePanels[view]) return;
  mobileWorkspaceView = view;
  for (const tab of workspaceTabs) {
    const selected = tab.dataset.workspaceTab === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  for (const [name, panel] of Object.entries(workspacePanels)) {
    panel.hidden = mobileWorkspace.matches && name !== view;
  }
  if (focusTab) byId(`workspace-tab-${view}`).focus();
}

function setNavigationOpen(open, focusToggle = false) {
  const toggle = byId("menu-toggle");
  const nav = byId("main-navigation");
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  nav.classList.toggle("is-open", open);
  if (!open && focusToggle) toggle.focus();
}

setMobileWorkspaceView(mobileWorkspaceView);
mobileWorkspace.addEventListener("change", () => {
  setMobileWorkspaceView(mobileWorkspaceView);
  if (!mobileWorkspace.matches) setNavigationOpen(false);
});

byId("menu-toggle").addEventListener("click", () => {
  const isOpen = byId("menu-toggle").getAttribute("aria-expanded") === "true";
  setNavigationOpen(!isOpen);
});
byId("main-navigation").addEventListener("click", (event) => {
  const link = event.target.closest("a");
  if (!link) return;
  if (link.dataset.requiresIndex && activeRepository && indexed) {
    setMobileWorkspaceView(link.dataset.requiresIndex === "diagram" ? "architecture" : "ask");
  }
  if (mobileWorkspace.matches) setNavigationOpen(false);
});
document.addEventListener("click", (event) => {
  if (byId("menu-toggle").getAttribute("aria-expanded") !== "true") return;
  if (!byId("main-navigation").contains(event.target) && !byId("menu-toggle").contains(event.target)) {
    setNavigationOpen(false);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && byId("menu-toggle").getAttribute("aria-expanded") === "true") {
    setNavigationOpen(false, true);
  }
});
for (const [index, tab] of workspaceTabs.entries()) {
  tab.addEventListener("click", () => setMobileWorkspaceView(tab.dataset.workspaceTab));
  tab.addEventListener("keydown", (event) => {
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % workspaceTabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (index + workspaceTabs.length - 1) % workspaceTabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = workspaceTabs.length - 1;
    else return;
    event.preventDefault();
    setMobileWorkspaceView(workspaceTabs[nextIndex].dataset.workspaceTab, true);
  });
}

function scrollTo(element, block = "start") {
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  element.scrollIntoView({ behavior, block });
}

function updateCurrentNavigation() {
  const currentHash = window.location.hash || "#repository";
  document.querySelectorAll(".primary-nav a").forEach((link) => {
    if (link.getAttribute("href") === currentHash) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  });
}

window.addEventListener("hashchange", updateCurrentNavigation);
updateCurrentNavigation();

repoInput.addEventListener("input", () => {
  repoInput.setCustomValidity(repoInput.value && !githubUrlPattern.test(repoInput.value)
    ? "Enter a GitHub URL in this format: https://github.com/owner/repository."
    : "");
});

document.querySelectorAll('a[data-requires-index]').forEach((link) => {
  link.addEventListener("click", (event) => {
    if (!activeRepository) {
      event.preventDefault();
      byId("index-status").textContent = "Enter a public GitHub repository URL to open this section.";
      byId("index-status").classList.remove("hidden");
      repoInput.focus();
      scrollTo(byId("repository"), "center");
      return;
    }
    if (!indexed) {
      event.preventDefault();
      byId("index-status").textContent = link.dataset.requiresIndex === "diagram"
        ? "Build the search index before generating the architecture diagram."
        : "Build the search index before asking questions.";
      byId("index-status").classList.remove("hidden");
      byId("index-button").focus();
      scrollTo(byId("index-button"), "center");
    }
  });
});

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
  button.setAttribute("aria-busy", "false");
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
  byId("diagram-downloads").classList.add("hidden");
  byId("diagram-sources").classList.add("hidden");
  byId("diagram-source-list").replaceChildren();
  currentDiagramSource = "";
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

function diagramFileName(extension) {
  const repositoryName = activeRepository.split("/").filter(Boolean).pop() || "repository";
  const safeName = repositoryName
    .replace(/\.git$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[.-]+|[.-]+$/g, "") || "repository";
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

function diagramSvgForExport() {
  const source = byId("diagram-output").querySelector("svg");
  if (!source || !currentDiagramSource) return null;
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
    if (format === "mermaid") {
      downloadBlob(new Blob([currentDiagramSource], { type: "text/plain;charset=utf-8" }), diagramFileName("mmd"));
      return;
    }
    const diagram = diagramSvgForExport();
    if (!diagram) return;
    if (format === "svg") {
      const content = new XMLSerializer().serializeToString(diagram.svg);
      downloadBlob(new Blob([content], { type: "image/svg+xml;charset=utf-8" }), diagramFileName("svg"));
      return;
    }

    const svgBlob = new Blob([new XMLSerializer().serializeToString(diagram.svg)], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(svgBlob);
    try {
      const image = new Image();
      image.src = url;
      await image.decode();
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = diagram.width * scale;
      canvas.height = diagram.height * scale;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("This browser cannot export the diagram image.");
      if (format === "jpeg") {
        context.fillStyle = "#111410";
        context.fillRect(0, 0, canvas.width, canvas.height);
      }
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve, reject) => {
        canvas.toBlob((result) => result ? resolve(result) : reject(new Error("Could not create the diagram image.")), `image/${format}`, 0.92);
      });
      downloadBlob(blob, diagramFileName(format === "jpeg" ? "jpg" : "png"));
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch (error) {
    byId("diagram-status").textContent = error.message || "Could not export the diagram.";
  }
}

async function copyDiagramMermaid() {
  if (!currentDiagramSource) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(currentDiagramSource);
    } else {
      const input = document.createElement("textarea");
      input.value = currentDiagramSource;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error("Clipboard access is unavailable in this browser.");
    }
    byId("diagram-status").textContent = "Mermaid source copied to the clipboard.";
  } catch (error) {
    byId("diagram-status").textContent = error.message || "Could not copy Mermaid source.";
  }
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
    currentDiagramSource = result.mermaid;
    byId("diagram-downloads").classList.remove("hidden");
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
  button.setAttribute("aria-busy", String(busy));
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
  if (hasIndex) line.textContent = `Index ready${fields.length ? ` · ${fields.join(", ")}` : ""}. Ask a question or generate the architecture diagram.`;
  else if (Number.isInteger(data.chunks)) line.textContent = "Index finished without searchable chunks. Check the indexing warnings or choose a repository with supported source files.";
  else if (Number.isInteger(files)) line.textContent = `Repository connected · ${files} source ${files === 1 ? "file" : "files"} found. Build the search index to enable questions and the architecture diagram.`;
  else line.textContent = "";
  line.classList.toggle("hidden", !line.textContent);
}

function updateQuestionEmptyState() {
  byId("empty-chat").classList.remove("hidden");
  byId("empty-chat").querySelector("p").textContent = indexed
    ? "Choose an example or ask about a file, function, or request flow."
    : "Build the search index to enable questions. Answers include source paths and line numbers.";
  document.querySelectorAll(".suggestion").forEach((button) => { button.disabled = !indexed; });
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
  updateQuestionEmptyState();
  renderFiles(data.files);
  repoPanel.classList.remove("hidden");
}

repoForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("scan-button");
  if (button.disabled) return;
  setNotice("");
  const path = repoInput.value.trim();
  if (!path) return;
  const previousRepository = activeRepository;
  const previousIndexStatus = byId("index-status").textContent;
  setBusy(button, true, "Checking repository…");
  byId("index-status").textContent = "Checking the public GitHub repository…";
  byId("index-status").classList.remove("hidden");
  try {
    const result = await api("/api/scan", { path });
    showRepository(result);
    scrollTo(repoPanel);
  } catch (error) {
    setNotice(error.message);
    if (previousRepository !== path) {
      activeRepository = "";
      indexed = false;
      repoPanel.classList.add("hidden");
      chatBody.replaceChildren(byId("empty-chat"));
      updateQuestionEmptyState();
      resetDiagram();
      questionInput.disabled = true;
      byId("ask-button").disabled = true;
      setIndexStatus({});
    } else {
      byId("index-status").textContent = previousIndexStatus;
      byId("index-status").classList.toggle("hidden", !previousIndexStatus);
    }
  } finally {
    setBusy(button, false, "Connect repository");
  }
});

byId("index-button").addEventListener("click", async () => {
  if (!activeRepository) return;
  const button = byId("index-button");
  if (button.disabled) return;
  const previousIndexStatus = byId("index-status").textContent;
  setBusy(button, true, "Indexing…");
  byId("index-progress").classList.remove("hidden");
  byId("index-progress").setAttribute("aria-busy", "true");
  byId("index-status").textContent = "Building the search index…";
  byId("index-status").classList.remove("hidden");
  setNotice("");
  byId("index-warnings").textContent = "";
  byId("index-warnings").classList.add("hidden");
  try {
    const result = await api("/api/index", { path: activeRepository });
    indexed = result.chunks > 0;
    byId("chunk-count").textContent = String(result.chunks);
    questionInput.disabled = !indexed;
    byId("ask-button").disabled = !indexed;
    document.querySelectorAll(".suggestion").forEach((suggestion) => { suggestion.disabled = !indexed; });
    updateDiagramAvailability();
    updateQuestionEmptyState();
    setIndexStatus(result);
    button.querySelector("span").textContent = indexed ? "Rebuild search index" : "Build search index";
    const warnings = result.warnings || [];
    const warningBox = byId("index-warnings");
    warningBox.textContent = warnings.join(" ");
    warningBox.classList.toggle("hidden", warnings.length === 0);
  } catch (error) {
    setNotice(error.message);
    byId("index-status").textContent = indexed
      ? "Index update failed. The current index is still available."
      : "Indexing failed. Resolve the error above, then try again.";
    if (previousIndexStatus && indexed) byId("index-status").textContent += ` ${previousIndexStatus}`;
    byId("index-status").classList.remove("hidden");
  } finally {
    byId("index-progress").classList.add("hidden");
    byId("index-progress").setAttribute("aria-busy", "false");
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
    button.querySelector("span").textContent = indexed ? "Rebuild search index" : "Build search index";
  }
});

byId("diagram-button").addEventListener("click", generateDiagram);
byId("download-diagram-png").addEventListener("click", () => downloadDiagram("png"));
byId("download-diagram-jpeg").addEventListener("click", () => downloadDiagram("jpeg"));
byId("download-diagram-svg").addEventListener("click", () => downloadDiagram("svg"));
byId("download-diagram-mermaid").addEventListener("click", () => downloadDiagram("mermaid"));
byId("copy-diagram-mermaid").addEventListener("click", copyDiagramMermaid);

byId("file-filter").addEventListener("input", (event) => {
  const filter = event.target.value.toLowerCase();
  for (const row of fileList.querySelectorAll(".file-row")) {
    row.classList.toggle("hidden", !row.lastElementChild.textContent.toLowerCase().includes(filter));
  }
});

function renderInlineText(container, text) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    container.append(document.createTextNode(text.slice(position, match.index)));
    const marker = match[0];
    const element = marker.startsWith("`") ? document.createElement("code") : document.createElement("strong");
    element.textContent = marker.startsWith("`") ? marker.slice(1, -1) : marker.slice(2, -2);
    container.append(element);
    position = match.index + marker.length;
  }
  container.append(document.createTextNode(text.slice(position)));
}

function renderAnswer(container, answer) {
  const lines = answer.split(/\r?\n/);
  let paragraph = [];
  let list = null;
  let codeLines = null;
  let codeLanguage = "";
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const block = document.createElement("p");
    renderInlineText(block, paragraph.join(" "));
    container.append(block);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    container.append(list);
    list = null;
  };
  for (const line of lines) {
    const fence = line.match(/^\s*```([\w+-]*)\s*$/);
    if (fence && codeLines === null) {
      flushParagraph();
      flushList();
      codeLines = [];
      codeLanguage = fence[1];
      continue;
    }
    if (fence && codeLines !== null) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (codeLanguage) code.className = `language-${codeLanguage}`;
      code.textContent = codeLines.join("\n");
      pre.append(code);
      container.append(pre);
      codeLines = null;
      codeLanguage = "";
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (!line.trim()) {
      flushParagraph();
      flushList();
    } else if (heading) {
      flushParagraph();
      flushList();
      const block = document.createElement("h3");
      renderInlineText(block, heading[1]);
      container.append(block);
    } else if (bullet || numbered) {
      flushParagraph();
      const type = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== type) {
        flushList();
        list = document.createElement(type);
      }
      const item = document.createElement("li");
      renderInlineText(item, (bullet || numbered)[1]);
      list.append(item);
    } else {
      flushList();
      paragraph.push(line.trim());
    }
  }
  if (codeLines !== null) {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    if (codeLanguage) code.className = `language-${codeLanguage}`;
    code.textContent = codeLines.join("\n");
    pre.append(code);
    container.append(pre);
  }
  flushParagraph();
  flushList();
}

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
  if (role === "assistant") renderAnswer(body, text);
  else body.textContent = text;
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
      const hasLine = Number.isInteger(source.start_line);
      const range = hasLine
        ? `:${source.start_line}${Number.isInteger(source.end_line) && source.end_line !== source.start_line ? `-${source.end_line}` : ""}`
        : "";
      summary.textContent = `${source.path}${range}`;
      const excerpt = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = source.content || "";
      excerpt.append(code);
      citation.append(summary, excerpt);
      if (["code", "documentation"].includes(source.source_type) && typeof source.source_url === "string") {
        try {
          const url = new URL(source.source_url);
          if (url.protocol === "https:" && url.hostname === "github.com") {
            const link = document.createElement("a");
            link.className = "citation-open-link";
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = "Open source on GitHub";
            citation.append(link);
          }
        } catch { /* Ignore malformed citation URLs. */ }
      }
      citations.append(citation);
    }
    wrapper.append(citations);
  }
  chatBody.append(wrapper);
  chatBody.scrollTop = chatBody.scrollHeight;
}

async function askQuestion(question) {
  if (!indexed || !question.trim() || byId("ask-button").disabled) return;
  byId("empty-chat").classList.add("hidden");
  appendMessage("user", question.trim());
  questionInput.value = "";
  const loading = document.createElement("div");
  loading.className = "message assistant";
  loading.innerHTML = '<div class="message-content"><div class="message-label">Searching repository files</div></div>';
  chatBody.append(loading);
  chatBody.scrollTop = chatBody.scrollHeight;
  byId("ask-button").disabled = true;
  byId("ask-button").setAttribute("aria-busy", "true");
  try {
    const result = await api("/api/ask", { path: activeRepository, question: question.trim(), top_k: 6 });
    loading.remove();
    if (typeof result.index_refreshed === "boolean") {
      const status = byId("index-status");
      const metrics = status.textContent.replace(/^Index ready(?: · )?/, "").replace(/\. Ask a question or generate the architecture diagram\.$/, "");
      const freshness = result.index_refreshed ? "Index refreshed from GitHub" : "Index current";
      status.textContent = ["Index ready", metrics, freshness].filter(Boolean).join(" · ");
      status.classList.remove("hidden");
    }
    const answer = result.answer.startsWith("Relevant code found:") && result.sources.length
      ? "GitContext retrieved these repository excerpts. Expand a citation to read the source text."
      : result.answer;
    appendMessage("assistant", answer, result.sources, result.retrieval_ms);
  } catch (error) {
    loading.remove();
    appendMessage("assistant", `The question could not be answered: ${error.message}\n\nCheck the connection and try again.`);
  } finally {
    byId("ask-button").disabled = !indexed;
    byId("ask-button").setAttribute("aria-busy", "false");
    questionInput.focus();
  }
}

byId("question-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (byId("ask-button").disabled) return;
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

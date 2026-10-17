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
    appendMessage("assistant", result.answer, result.sources, result.retrieval_ms);
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

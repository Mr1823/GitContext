# GitContext backend architecture

## Audit summary

The repository is a Python modular monolith in `src/gitcontext/`. It does not
contain the illustrative `backend/application/infrastructure` directory tree.
The FastAPI app and Typer CLI are entry points over shared indexing, retrieval,
and storage modules. The package has no separate domain, repository-interface,
or application-service directories.

An AST scan of internal Python imports found no strongly connected components.
Runtime calls are separate from imports: for example, `web.py` calls the
indexing workflow, but that call is not evidence of a circular import. The
indexing path is coordinated by `indexer.py`; API handlers remain in
`web.py`. A dependency-neutral `records.py` now owns `ParsedBlock`, so parser,
indexing, GitHub/history ingestion, and vector storage share the record without
storage importing the parser implementation. `ingest.parser.ParsedBlock`
remains available as a compatibility import.

Supported-file read and UTF-8 decode errors now fail indexing with the relative
path in the error. Previously an unreadable file was counted as skipped, which
could leave old chunks in place while returning a successful summary. Changes
still persist one file at a time; a later storage or embedding failure can leave
earlier file updates committed. Full indexing transactions across SQLite and
Qdrant remain technical debt.

GitHub transport failures now use a distinct `GitHubAPIError`. The web API maps
invalid repository input to 400, GitHub upstream failures to 502, and local
index/storage failures to 500. Upstream response bodies are not returned to API
clients.

## Diagram 1: Package responsibilities

Arrows in this diagram show runtime flow. The nodes name actual modules or
module groups in the repository.

```mermaid
flowchart TB
  subgraph presentation["Presentation"]
    cli["CLI commands [cli.py]"]
    api["HTTP routes and app [web.py]"]
  end

  subgraph application["Application workflows"]
    indexer["Repository indexing [indexer.py]"]
    retrieval["Hybrid retrieval [retrieval.py]"]
    answer["Grounded answer generation [answer.py]"]
    diagram["Architecture diagram generation [diagram.py]"]
  end

  subgraph ingest["Repository ingestion and parsing"]
    remote["GitHub snapshots [ingest/remote.py]"]
    source["Local repository scan [ingest/repository.py]"]
    github["GitHub history and discussions [ingest/github.py]"]
    history["Local Git history [ingest/history.py]"]
    parser["Source and docs parsing [ingest/parser.py]"]
    chunks["Chunking [chunking.py]"]
    records["Shared passage record [records.py]"]
  end

  subgraph adapters["Provider and persistence adapters"]
    embeddings["Optional local embeddings [embeddings.py]"]
    store["SQLite metadata and local Qdrant [vector_store.py]"]
    settings["Environment settings [config.py]"]
  end

  cli --> indexer
  api --> indexer
  api --> retrieval
  api --> answer
  api --> diagram
  indexer --> remote
  indexer --> source
  indexer --> github
  indexer --> history
  indexer --> chunks
  chunks --> parser
  parser --> records
  github --> records
  history --> records
  indexer --> embeddings
  indexer --> store
  retrieval --> store
  retrieval --> embeddings
  answer --> settings
  diagram --> retrieval
  diagram --> store
```

## Diagram 2: Actual import dependencies

Arrows mean Python imports, including the CLI's lazy import of `web.py` inside
the `web` command. They do not represent runtime data flow. No cycles were found
in the checked internal import graph.

```mermaid
flowchart TB
  cli["CLI [cli.py]"]
  web["HTTP app [web.py]"]
  indexer["Index workflow [indexer.py]"]
  diagram["Diagram builder [diagram.py]"]
  retrieval["Retrieval [retrieval.py]"]
  answer["Answer generation [answer.py]"]
  chunking["Chunking [chunking.py]"]
  parser["Parsing [ingest/parser.py]"]
  source["Repository scan [ingest/repository.py]"]
  remote["GitHub snapshots [ingest/remote.py]"]
  github["GitHub context [ingest/github.py]"]
  history["Git history [ingest/history.py]"]
  provider_errors["GitHub errors [ingest/errors.py]"]
  records["Shared records [records.py]"]
  embeddings["Embeddings and reranking [embeddings.py]"]
  store["SQLite and Qdrant [vector_store.py]"]
  config["Settings [config.py]"]
  evaluation["Metrics [evaluation.py]"]

  cli --> answer
  cli --> config
  cli --> diagram
  cli --> evaluation
  cli --> indexer
  cli --> remote
  cli --> source
  cli --> retrieval
  cli --> store
  cli --> web
  web --> answer
  web --> config
  web --> diagram
  web --> indexer
  web --> remote
  web --> provider_errors
  web --> retrieval
  web --> store
  indexer --> chunking
  indexer --> config
  indexer --> embeddings
  indexer --> github
  indexer --> history
  indexer --> remote
  indexer --> source
  indexer --> records
  indexer --> store
  chunking --> parser
  chunking --> records
  parser --> records
  remote --> provider_errors
  remote --> source
  github --> provider_errors
  github --> records
  history --> records
  retrieval --> embeddings
  retrieval --> store
  store --> records
  answer --> config
  diagram --> answer
  diagram --> config
  diagram --> retrieval
  diagram --> store
```

## Diagram 3: Runtime workflows

This diagram reflects the current implementation. GitHub discussion ingestion is
optional and can produce warnings while code indexing succeeds. Semantic search
and reranking are optional. There is no graph lookup stage or citation-validation
stage in question answering.

```mermaid
flowchart TB
  subgraph indexing["Repository indexing"]
    input["Repository URL or local path"] --> entry["CLI or POST /api/index"]
    entry --> validate["Validate input and resolve repository"]
    validate --> fetch["Fetch GitHub metadata and source snapshot"]
    validate --> scan["Scan supported local files"]
    fetch --> scan
    scan --> parse["Parse and chunk source and documentation"]
    parse --> enrich["Read local Git history or remote GitHub commits, PRs and incidents"]
    parse --> embed["Generate vectors when an embedding model is configured"]
    enrich --> persist["Store passages and metadata in SQLite"]
    embed --> persist
    persist --> sync["Sync dense vectors to local Qdrant when present"]
    sync --> complete["Return indexing summary and warnings"]
  end

  subgraph answering["Question answering"]
    question["Question and repository"] --> qentry["CLI ask or POST /api/ask"]
    qentry --> freshness["Check local hashes or fetch GitHub head status"]
    freshness -->|"stale remote index"| reindex["Refresh remote snapshot and index"]
    freshness --> retrieve["Retrieve indexed passages"]
    reindex --> retrieve
    retrieve --> bm25["BM25 lexical ranking"]
    retrieve --> vector["Optional Qdrant vector search"]
    bm25 --> rank["Combine candidates"]
    vector --> rank
    rank --> rerank["Optional cross-encoder reranking"]
    rerank --> llm["Use configured LLM when a key and evidence exist"]
    rerank --> fallback["Return cited evidence excerpt otherwise"]
    llm --> response["Answer plus source metadata"]
    fallback --> response
  end
```

## Verified behavior and remaining risks

- The API currently accepts public GitHub URLs. The CLI also accepts local
  folders. `/api/ask` checks GitHub freshness and can download and re-index a
  changed repository before answering; repeated requests therefore make GitHub
  API calls and can trigger costly indexing. Rate limiting and per-repository
  refresh coordination are not implemented here.
- Retrieval always uses BM25. Qdrant vector search requires an embedding model
  and stored vectors. A cross-encoder reranker is used only when configured.
- The answer prompt asks the model to cite retrieved paths and line ranges, but
  generated claims are not validated against the source list. The API's
  separate `sources` field is populated from retrieval results.
- SQLite metadata and file hashes are the durable index records. Qdrant is a
  local dense-vector collection rebuilt by `sync_qdrant`; it is not a separate
  graph database. There is no persisted code graph or graph-based retrieval.
- Settings are loaded from environment variables when requested. Model loading
  is lazy and cached; importing the API module creates the FastAPI app but does
  not fetch repositories or load embedding models.

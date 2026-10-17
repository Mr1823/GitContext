# GitContext

GitContext is a local codebase search and question-answering CLI. It scans source
files, breaks them into line-addressable chunks, indexes them in SQLite, and
retrieves relevant code for a question. Answers include file and line citations.

## Install

Python 3.12 or 3.13 is required.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
```

## Use

```bash
gitcontext --help
gitcontext hello
gitcontext scan /path/to/repository
gitcontext index /path/to/repository
gitcontext ask "Where is authentication handled?" --path /path/to/repository
gitcontext web
```

`gitcontext web` opens the browser interface at `http://127.0.0.1:8765`. In the
web UI, paste the repository path, scan it, build the index, then ask questions
in the chat panel. The server binds to localhost by default. Use `--host` and
`--port` to choose another address or port; only bind beyond localhost on a
network you trust.

The index is stored under `<repository>/.gitcontext/index.sqlite` and is ignored
by Git. Re-run `index` after changing source files. The current scanner supports
Python, JavaScript, JSX, TypeScript, TSX, and Java. Python functions, async
functions, and classes are indexed as named blocks; other files are indexed in
overlapping line windows.

By default, GitContext uses BM25 lexical retrieval and answers with the most
relevant source excerpts. This works offline and does not require a model key.
To enable semantic retrieval, install the semantic extra and configure a
Sentence Transformers model before indexing (the model is downloaded on first
use):

```bash
python -m pip install -e '.[semantic]'
export GITCONTEXT_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
gitcontext index /path/to/repository
```

To synthesize an answer from retrieved excerpts, set an OpenAI-compatible API
key. The default endpoint is OpenAI; compatible providers can be selected with
`OPENAI_BASE_URL` and `GITCONTEXT_LLM_MODEL`.

```bash
export OPENAI_API_KEY=...
export GITCONTEXT_LLM_MODEL=gpt-4o-mini
gitcontext ask "How does the CLI register commands?" --path /path/to/repository
```

Set `GITCONTEXT_DATABASE` to use a database outside the repository. Do not put
secrets in source control; `.env` is ignored.

## Development

```bash
pytest
ruff check src tests
```

This first complete version provides source scanning, chunking, persistent
indexing, hybrid retrieval, and grounded answers. It does not yet support
incremental indexing, language-specific parsing outside Python, or repository
editing.

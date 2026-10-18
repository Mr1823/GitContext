"""Grounded answer generation with an optional OpenAI-compatible endpoint."""

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gitcontext.config import Settings


def format_evidence_answer(results: list[dict[str, Any]]) -> str:
    """Provide useful source excerpts when no language model is configured."""
    if not results:
        return "I couldn't find matching code in the index. Try different search terms or re-index the repository."
    sections = ["Relevant code found:"]
    for result in results[:4]:
        citation = f"{result['path']}:{result['start_line']}-{result['end_line']}"
        provenance = []
        if result.get("author"):
            provenance.append(f"author: {result['author']}")
        if result.get("commit_sha"):
            provenance.append(f"commit: {result['commit_sha'][:12]}")
        if result.get("last_modified"):
            provenance.append(f"updated: {result['last_modified']}")
        if result.get("pr_number"):
            provenance.append(f"PR #{result['pr_number']}")
        if result.get("source_url"):
            provenance.append(result["source_url"])
        if provenance:
            citation += " (" + "; ".join(provenance) + ")"
        snippet = result["content"].strip()
        if len(snippet) > 900:
            snippet = snippet[:900].rstrip() + " …"
        sections.append(f"\n**{citation}**\n```\n{snippet}\n```")
    sections.append("\nSet `OPENAI_API_KEY` to have GitContext synthesize an answer from these excerpts.")
    return "\n".join(sections)


def complete_json(prompt: str, settings: Settings) -> str:
    """Request JSON text from the configured OpenAI-compatible chat endpoint."""
    if not settings.llm_api_key:
        raise RuntimeError("Set OPENAI_API_KEY to use structured generation.")
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Return one JSON object and no prose."},
            {"role": "user", "content": prompt},
        ],
    }
    request = Request(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read())
        return data["choices"][0]["message"]["content"].strip()
    except HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Answer service returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not generate JSON: {error}") from error


def answer_question(
    question: str,
    results: list[dict[str, Any]],
    settings: Settings,
) -> str:
    """Answer from retrieved snippets and keep source citations explicit."""
    if not settings.llm_api_key:
        return format_evidence_answer(results)
    if not results:
        return format_evidence_answer(results)
    context = "\n\n".join(
        f"SOURCE {item.get('source_type', 'code')} {item['path']}:{item['start_line']}-{item['end_line']} "
        f"commit={item.get('commit_sha')} author={item.get('author')} "
        f"modified={item.get('last_modified')} pr={item.get('pr_number')} "
        f"url={item.get('source_url')}\n{item['content']}"
        for item in results
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": (
                "Answer the user's question using only the supplied repository excerpts. "
                "Cite every factual claim with its exact SOURCE path and line range. "
                "When relevant, include commit, author, date, PR number, or source URL "
                "If the excerpts do not establish an answer, say what is missing."
            )},
            {"role": "user", "content": f"Question: {question}\n\nExcerpts:\n{context}"},
        ],
    }
    request = Request(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read())
        return data["choices"][0]["message"]["content"].strip()
    except HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Answer service returned HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not generate an answer: {error}") from error

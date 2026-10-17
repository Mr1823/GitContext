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
        snippet = result["content"].strip()
        if len(snippet) > 900:
            snippet = snippet[:900].rstrip() + " …"
        sections.append(f"\n**{citation}**\n```\n{snippet}\n```")
    sections.append("\nSet `OPENAI_API_KEY` to have GitContext synthesize an answer from these excerpts.")
    return "\n".join(sections)


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
        f"SOURCE {item['path']}:{item['start_line']}-{item['end_line']}\n{item['content']}"
        for item in results
    )
    payload = {
        "model": settings.llm_model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": (
                "Answer the user's question using only the supplied repository excerpts. "
                "Cite every factual claim with its exact SOURCE path and line range. "
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

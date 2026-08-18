"""Tool discovery shared by Python runtimes."""

import json
import math
import re
from collections import Counter
from typing import Any, Protocol

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_BM25_K1 = 1.2
_BM25_B = 0.75


class SearchableTool(Protocol):
    """Tool metadata required by the runtime-neutral search policy."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any]: ...

    @property
    def output_schema(self) -> dict[str, Any] | None: ...

    @property
    def timeout_ms(self) -> int: ...


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _score_field(query: set[str], documents: list[list[str]], index: int) -> float:
    if not documents:
        return 0

    document = documents[index]
    frequencies = Counter(document)
    average_length = sum(len(item) for item in documents) / len(documents)
    normalized_average = average_length if average_length > 0 else 1
    score = 0.0

    for term in query:
        term_frequency = frequencies[term]
        document_frequency = sum(term in item for item in documents)
        if term_frequency == 0 or document_frequency == 0:
            continue
        inverse_frequency = math.log(
            1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        numerator = term_frequency * (_BM25_K1 + 1)
        denominator = term_frequency + _BM25_K1 * (
            1 - _BM25_B + _BM25_B * (len(document) / normalized_average)
        )
        score += inverse_frequency * (numerator / denominator)

    return score


def search_tools(
    tools: list[SearchableTool],
    query: str,
    detail: str = "brief",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rank tool definitions using the same BM25 policy as Pyodide."""
    if not isinstance(query, str):
        raise TypeError("tool_search expects a string query")
    if detail not in {"brief", "full"}:
        raise ValueError("tool_search detail must be 'brief' or 'full'")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("tool_search limit must be an integer")
    if limit < 1:
        raise ValueError("tool_search limit must be at least 1")

    query_tokens = set(_tokens(query))
    if not query_tokens:
        return []

    fields = [
        ([_tokens(tool.name) for tool in tools], 3.5),
        ([_tokens(tool.description) for tool in tools], 1.5),
        (
            [
                _tokens(
                    f"{json.dumps(tool.input_schema)} {json.dumps(tool.output_schema)}"
                )
                for tool in tools
            ],
            0.35,
        ),
    ]
    scored = [
        (
            sum(
                weight * _score_field(query_tokens, documents, index)
                for documents, weight in fields
            ),
            index,
            tool,
        )
        for index, tool in enumerate(tools)
    ]
    matches = [entry for entry in scored if entry[0] > 0]
    matches.sort(key=lambda entry: (-entry[0], entry[1]))
    if not matches:
        return []

    minimum_score = matches[0][0] * 0.15
    results: list[dict[str, Any]] = []
    for score, _, tool in matches:
        if score < minimum_score or len(results) >= limit:
            continue
        result: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "score": round(score, 4),
        }
        if detail == "full":
            result.update(
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                timeout_ms=tool.timeout_ms,
            )
        results.append(result)
    return results

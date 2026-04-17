"""Maximal Marginal Relevance (MMR) reranking."""
import re
from typing import Any, Dict, List, Set


def tokenize(text: str) -> Set[str]:
    """Simple whitespace + punctuation tokenizer."""
    return set(re.findall(r"\w+", text.lower()))


def jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    """Calculate Jaccard similarity between two token sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def mmr_rerank(
    results: List[Dict[str, Any]],
    lambda_param: float = 0.7,
    content_key: str = "content",
    score_key: str = "score",
) -> List[Dict[str, Any]]:
    """Apply MMR reranking to search results.

    MMR = λ * relevance - (1-λ) * max_similarity_to_selected

    Args:
        results: List of search result dicts.
        lambda_param: Trade-off between relevance and diversity (0-1).
        content_key: Key for text content in result dicts.
        score_key: Key for relevance score in result dicts.

    Returns:
        Reranked list of results with improved diversity.
    """
    if not results or len(results) <= 1:
        return results

    # Pre-tokenize all content
    token_cache: Dict[int, Set[str]] = {}
    for index, item in enumerate(results):
        text = item.get(content_key, "")
        token_cache[index] = tokenize(text)

    # Normalize scores to [0, 1]
    scores = [item.get(score_key, 0.0) for item in results]
    max_score = max(scores) if scores else 1.0
    min_score = min(scores) if scores else 0.0
    score_range = max_score - min_score if max_score != min_score else 1.0

    selected_indices: List[int] = []
    remaining = set(range(len(results)))
    reranked: List[Dict[str, Any]] = []

    while remaining:
        best_idx = None
        best_mmr = float("-inf")

        for idx in remaining:
            normalized_relevance = (scores[idx] - min_score) / score_range

            # Max similarity to already selected items
            max_sim = 0.0
            for sel_idx in selected_indices:
                similarity = jaccard_similarity(token_cache[idx], token_cache[sel_idx])
                max_sim = max(max_sim, similarity)

            mmr_score = lambda_param * normalized_relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = idx

        if best_idx is not None:
            selected_indices.append(best_idx)
            remaining.discard(best_idx)
            reranked.append(results[best_idx])

    return reranked

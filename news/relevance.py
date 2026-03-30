"""
Article relevance scoring: match articles to a market question.

Stage 1 (always active): keyword extraction + case-insensitive title/body match.
Stage 2 (optional, USE_SEMANTIC_RELEVANCE=True): sentence-transformers cosine similarity.

All functions are pure — no I/O, no side effects.
"""
from __future__ import annotations

import re
from functools import lru_cache

from config.schemas import NewsArticle
from config.settings import settings

# Common English stopwords to exclude from keyword extraction
_STOPWORDS = frozenset(
    "a an the and or but if in on at to for of with by from is are was were "
    "be been being have has had do does did will would could should may might "
    "shall can it its this that these those what which who whom when where why "
    "how all each both few more most other some such no nor not only own same so "
    "than too very just because as until while about against between into through "
    "during before after above below up down out off over under again then once "
    "here there any there their they them i we you he she it we they s t".split()
)


def extract_keywords(question: str) -> list[str]:
    """Extract meaningful keywords from a market question.

    Returns a deduplicated list of lowercase keywords, excluding stopwords
    and tokens shorter than 3 characters.

    Example:
        >>> extract_keywords("Will Bitcoin exceed $120k by end of Q2 2025?")
        ['bitcoin', 'exceed', '120k', 'q2', '2025']
    """
    # Keep alphanumeric tokens and dollar amounts
    tokens = re.findall(r"\$?[\w]+", question.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for tok in tokens:
        clean = tok.lstrip("$")
        if clean and clean not in _STOPWORDS and len(clean) >= 3 and clean not in seen:
            seen.add(clean)
            keywords.append(clean)
    return keywords


def keyword_match_score(
    article: NewsArticle,
    keywords: list[str],
) -> float:
    """Score an article by keyword coverage (0.0–1.0).

    Returns the fraction of keywords found in the article title + body.
    A score of 0.0 means no keywords matched.
    """
    if not keywords:
        return 0.0

    haystack = (article.title + " " + (article.body or "")).lower()
    matched = sum(1 for kw in keywords if kw in haystack)
    return matched / len(keywords)


def filter_by_keywords(
    articles: list[NewsArticle],
    keywords: list[str],
    min_score: float = 0.0,
) -> list[NewsArticle]:
    """Return articles that contain at least one keyword, scored and sorted.

    Updates each article's relevance_score in place.
    Articles with score <= min_score are excluded.
    """
    scored: list[tuple[float, NewsArticle]] = []
    for article in articles:
        score = keyword_match_score(article, keywords)
        if score > min_score:
            # Pydantic v2: use model_copy to create updated instance
            scored.append((score, article.model_copy(update={"relevance_score": score})))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored]


@lru_cache(maxsize=1)
def _get_semantic_model():
    """Lazy-load the sentence-transformers model (singleton, cached)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")


def _cosine_similarity(a, b) -> float:
    """Compute cosine similarity between two numpy vectors."""
    import numpy as np

    dot = float(np.dot(a, b))
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return dot / norm if norm > 0 else 0.0


def semantic_score(question: str, article: NewsArticle) -> float:
    """Compute cosine similarity between question and article title embeddings.

    Requires USE_SEMANTIC_RELEVANCE=True and sentence-transformers installed.
    Returns a score in [0, 1].
    """
    model = _get_semantic_model()
    import numpy as np

    q_emb = model.encode(question, convert_to_numpy=True)
    t_emb = model.encode(article.title, convert_to_numpy=True)
    score = _cosine_similarity(q_emb, t_emb)
    return max(0.0, min(1.0, float(score)))


class RelevanceScorer:
    """Scores articles against a market question using keyword + optional semantic matching."""

    def __init__(self, use_semantic: bool | None = None) -> None:
        self._use_semantic = (
            use_semantic if use_semantic is not None else settings.use_semantic_relevance
        )

    def score(self, question: str, article_title: str, article_body: str = "") -> float:
        """Score a single article (by title + body text) against a question.

        Returns a relevance score in [0, 1].
        """
        dummy = NewsArticle(
            source="",
            title=article_title,
            body=article_body or None,
            published_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        keywords = extract_keywords(question)
        kw_score = keyword_match_score(dummy, keywords)

        if self._use_semantic and kw_score > 0:
            sem_score = semantic_score(question, dummy)
            return (kw_score + sem_score) / 2.0

        return kw_score

    def rank(
        self,
        question: str,
        articles: list[NewsArticle],
        min_score: float | None = None,
    ) -> list[NewsArticle]:
        """Rank and filter articles by relevance to a market question.

        Returns articles sorted by relevance_score descending, filtered by
        min_score (defaults to settings.news_min_relevance_score).
        """
        threshold = min_score if min_score is not None else settings.news_min_relevance_score
        keywords = extract_keywords(question)

        # Stage 1: keyword filter
        candidates = filter_by_keywords(articles, keywords, min_score=0.0)

        if not self._use_semantic:
            return [a for a in candidates if a.relevance_score >= threshold]

        # Stage 2: semantic re-scoring
        rescored: list[tuple[float, NewsArticle]] = []
        for article in candidates:
            sem = semantic_score(question, article)
            combined = (article.relevance_score + sem) / 2.0
            if combined >= threshold:
                rescored.append((combined, article.model_copy(update={"relevance_score": combined})))

        rescored.sort(key=lambda x: x[0], reverse=True)
        return [a for _, a in rescored]

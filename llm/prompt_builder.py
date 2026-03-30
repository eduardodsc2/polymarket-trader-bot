"""
Build LLM prompts from market data and news context.

Loads category-specific prompt templates from llm/prompts/.
All functions are pure — no I/O beyond reading template files at init.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from config.schemas import NewsArticle

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Maps Gamma API category → template filename (without .txt)
_CATEGORY_MAP: dict[str, str] = {
    "crypto":       "crypto",
    "bitcoin":      "crypto",
    "ethereum":     "crypto",
    "defi":         "crypto",
    "politics":     "politics",
    "elections":    "politics",
    "sports":       "sports",
    "soccer":       "sports",
    "nfl":          "sports",
    "nba":          "sports",
    "ai":           "ai_tech",
    "tech":         "ai_tech",
    "technology":   "ai_tech",
}


def _load_template(category: str) -> str:
    """Load the prompt template for a market category.

    Falls back to base.txt if no category-specific template exists.
    Templates are read from disk once per call — caller should cache if needed.
    """
    template_name = _CATEGORY_MAP.get((category or "").lower(), "base")
    path = _PROMPTS_DIR / f"{template_name}.txt"
    if not path.exists():
        path = _PROMPTS_DIR / "base.txt"
    return path.read_text(encoding="utf-8")


def _format_news_context(articles: list[NewsArticle]) -> str:
    """Format a list of articles into a numbered context block for the prompt.

    Format per article:
        [N] SOURCE | TITLE | DATE
        SUMMARY (truncated)
    """
    if not articles:
        return "No recent news available for this market."

    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        date_str = art.published_at.strftime("%Y-%m-%d")
        header = f"[{i}] {art.source.upper()} | {art.title} | {date_str}"
        lines.append(header)
        if art.body:
            lines.append(art.body)
        lines.append("")

    return "\n".join(lines).strip()


def build_prompt(
    question: str,
    category: str,
    resolution_date: str,
    current_price: float,
    articles: list[NewsArticle] | None = None,
) -> str:
    """Build a formatted LLM prompt for a Polymarket market.

    Pure function — same inputs always produce the same output.

    Args:
        question: The market question text.
        category: Market category (used to select the prompt template).
        resolution_date: Human-readable resolution date string (e.g. "2024-06-30").
        current_price: Current YES token price (0.0–1.0).
        articles: List of relevant news articles to include as context.

    Returns:
        Formatted prompt string ready to send to the LLM.
    """
    template = _load_template(category)
    news_context = _format_news_context(articles or [])

    return template.format(
        question=question,
        category=category or "general",
        resolution_date=resolution_date,
        current_price=current_price,
        news_context=news_context,
    )


class PromptBuilder:
    """Stateless wrapper for DI compatibility."""

    def build(
        self,
        question: str,
        market_price: float,
        news_context: str,
        category: str = "base",
        resolution_date: str = "unknown",
        articles: list[NewsArticle] | None = None,
    ) -> str:
        """Build prompt. If articles are provided, they take precedence over news_context str."""
        if articles is not None:
            return build_prompt(question, category, resolution_date, market_price, articles)

        # Legacy: news_context passed as pre-formatted string
        template = _load_template(category)
        return template.format(
            question=question,
            category=category,
            resolution_date=resolution_date,
            current_price=market_price,
            news_context=news_context or "No recent news available for this market.",
        )

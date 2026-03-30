"""
LLM probability estimator — queries Claude with news context.

Enforces:
- Cache hit: skips API call if same (condition_id, prompt_hash) within TTL
- Daily spend limit: raises RuntimeError if LLM_DAILY_SPEND_LIMIT_USD exceeded
- Parse failure: raises ValueError (never returns partial/invalid estimates)
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger

from config.schemas import LLMEstimate, NewsArticle
from config.settings import settings
from llm.cache import LLMCache
from llm.prompt_builder import build_prompt
from llm.response_parser import parse_response, prompt_hash

# Cost estimate per token for claude-sonnet-4-6 (USD)
# Input: ~$3.00 / 1M tokens, Output: ~$15.00 / 1M tokens
_COST_PER_INPUT_TOKEN  = 3.00 / 1_000_000
_COST_PER_OUTPUT_TOKEN = 15.00 / 1_000_000


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens * _COST_PER_INPUT_TOKEN + output_tokens * _COST_PER_OUTPUT_TOKEN,
        6,
    )


class LLMEstimator:
    """Queries Claude to estimate the probability a market resolves YES.

    Dependencies are injected via constructor for testability.
    """

    def __init__(
        self,
        cache: LLMCache | None = None,
        api_key: str = "",
        model: str = "claude-sonnet-4-6",
    ) -> None:
        self._cache = cache or LLMCache()
        self._api_key = api_key or settings.anthropic_api_key
        self._model = model
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def estimate(
        self,
        condition_id: str,
        question: str,
        category: str,
        resolution_date: str,
        current_price: float,
        articles: list[NewsArticle] | None = None,
    ) -> LLMEstimate:
        """Estimate probability that a market resolves YES.

        Args:
            condition_id: Polymarket market condition ID.
            question: Market question text.
            category: Market category (selects prompt template).
            resolution_date: Human-readable resolution date (e.g. "2024-06-30").
            current_price: Current YES token price (0.0–1.0).
            articles: Relevant news articles for context.

        Returns:
            LLMEstimate with probability, confidence, reasoning, sources.

        Raises:
            RuntimeError: If daily spend limit is reached.
            ValueError: If LLM response cannot be parsed.
        """
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        prompt = build_prompt(question, category, resolution_date, current_price, articles or [])
        phash = prompt_hash(prompt)

        # 1. Cache hit
        cached = self._cache.get(condition_id, phash)
        if cached is not None:
            logger.debug(
                "LLMEstimator: cache hit",
                condition_id=condition_id,
                probability=cached.probability,
            )
            return cached

        # 2. Daily spend guard
        daily_cost = self._cache.get_daily_cost()
        if daily_cost >= settings.llm_daily_spend_limit_usd:
            raise RuntimeError(
                f"Daily LLM spend limit reached: ${daily_cost:.4f} >= "
                f"${settings.llm_daily_spend_limit_usd:.2f}"
            )

        # 3. API call
        logger.info(
            "LLMEstimator: calling Claude API",
            condition_id=condition_id,
            category=category,
            articles=len(articles or []),
            daily_cost_so_far=daily_cost,
        )

        client = self._get_client()
        response = client.messages.create(
            model=self._model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text
        cost = _estimate_cost(response.usage.input_tokens, response.usage.output_tokens)

        logger.debug(
            "LLMEstimator: API response received",
            condition_id=condition_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost,
        )

        # 4. Parse (raises ValueError on bad format)
        estimate = parse_response(raw_text, condition_id, self._model, prompt)

        # 5. Cache and return
        self._cache.save(estimate, estimated_cost_usd=cost)
        return estimate

    # Legacy interface used by old stub callers
    def estimate_legacy(self, condition_id: str, question: str, news_context: str) -> LLMEstimate:
        """Legacy interface — prefer estimate() with structured articles."""
        return self.estimate(
            condition_id=condition_id,
            question=question,
            category="base",
            resolution_date="unknown",
            current_price=0.5,
            articles=None,
        )

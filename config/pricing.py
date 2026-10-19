"""What each model costs, and the one function that turns an API response's usage into dollars.

Prices are $ per 1M tokens (Anthropic first-party API). Update here when prices change.
"""

PRICES = {  # model: (input, output)
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100K tokens
}

# Billing rates for prompt caching, as multipliers on the input price:
# writing a prefix into the cache costs 1.25x, reading it back costs 0.1x (5-minute cache).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def cost(model: str, input_tokens: int, output_tokens: int,
         cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """Dollar cost of a request. input_tokens are the uncached input tokens, as the API reports them."""
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * p_in
            + cache_write_tokens * p_in * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * p_in * CACHE_READ_MULTIPLIER
            + output_tokens * p_out) / 1e6


def usage_tokens(usage) -> dict:
    """The four token counts from an API response's usage (cache fields are None when caching wasn't used)."""
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None) or 0}

"""Which Claude model does which job. Prices per model are in pricing.py."""

# Extraction: the model whose extractions the agent reads. The full extraction must be run with it,
# with the prompt version in extraction/schema.py (PROMPT_VERSION).
EXTRACTION_MODEL = "claude-opus-5"

# Agent: the models the CLI and the web page offer, and their defaults.
AGENT_MODELS = ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5-5"]
AGENT_DEFAULT_CLI = "claude-opus-5"
AGENT_DEFAULT_WEB = "claude-sonnet-5-5"

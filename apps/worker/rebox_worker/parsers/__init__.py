"""AI parsers: free text already in the raw store (bios, posts, articles) -> evidence-backed claims.

See ``docs/PARSING.md``. ``base.ParserConnector`` is the shared connector; ``llm`` the model
client; ``prompt`` / ``schema`` the prompt and structured-output tool; ``extract`` the pure
output -> claims step with the evidence, privacy and grounding guards.
"""

from .base import ParserConnector
from .extract import extract_claims
from .llm import AnthropicModel, Usage
from .prompt import PROMPT_VERSION, InputDoc

__all__ = [
    "PROMPT_VERSION",
    "AnthropicModel",
    "InputDoc",
    "ParserConnector",
    "Usage",
    "extract_claims",
]

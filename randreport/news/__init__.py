"""News extraction: strict schema, no mandatory provider (docs/REQUIREMENTS.md section 8).

The model is a pure function over untrusted public text: it receives no tools, no
database handle and no secrets. Its output is re-validated here, and every public
link is built by this application rather than taken from the model.
"""

from ..news.extract import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    BatchOutcome,
    LlmProvider,
    NewsExtractor,
    build_prompt,
)
from ..news.models import (
    ALLOWED_FIELDS,
    Classification,
    Extraction,
    ExtractionInvalid,
    NewsItem,
    build_news_item,
    parse_extraction,
)

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "BatchOutcome",
    "LlmProvider",
    "NewsExtractor",
    "build_prompt",
    "ALLOWED_FIELDS",
    "Classification",
    "Extraction",
    "ExtractionInvalid",
    "NewsItem",
    "build_news_item",
    "parse_extraction",
]

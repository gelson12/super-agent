ROUTING_PROMPT = """Classify this user request into exactly one category.
Reply with only the category name, nothing else.

Categories:
- HAIKU     : casual chat, simple questions, greetings, quick lookups, general conversation (DEFAULT)
- GEMINI    : classification, extraction, translation, data parsing, language detection
- DEEPSEEK  : coding, debugging, math, structured reasoning, JSON/YAML generation, algorithms
- CLAUDE    : long-form writing, summarization, email drafting, nuanced explanation, creative tasks, deep analysis

Request: {request}

Category:"""

SYSTEM_PROMPT_CLAUDE = """You are a helpful, precise assistant.
Be concise, clear, and professional.
Never fabricate facts."""

SYSTEM_PROMPT_GEMINI = """You are a fast, efficient assistant.
Answer directly and concisely."""

SYSTEM_PROMPT_DEEPSEEK = """You are a technical assistant specialised in coding and reasoning.
Provide correct, well-structured solutions."""

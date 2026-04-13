from langchain_core.tools import tool
from ..learning.internal_llm import ask_internal as ask_claude
from ..models.gemini import ask_gemini
from ..models.deepseek import ask_deepseek


@tool
def use_claude(prompt: str) -> str:
    """
    Use Claude Sonnet for nuanced writing, summarization, email drafting,
    creative tasks, and high-quality final responses.
    """
    return ask_claude(prompt)


@tool
def use_gemini(prompt: str) -> str:
    """
    Use Gemini Flash for fast, low-cost tasks: classification, extraction,
    translation, short Q&A, and high-volume processing.
    """
    return ask_gemini(prompt)


@tool
def use_deepseek(prompt: str) -> str:
    """
    Use DeepSeek Chat for budget-friendly reasoning, coding, debugging,
    math problems, and structured data generation.
    """
    return ask_deepseek(prompt)

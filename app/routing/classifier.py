"""
classifier.py — Parallel multi-model request classifier.
Fully bilingual: English + Portuguese (all models understand both natively).

CHANGES FROM ORIGINAL:
- Haiku is PRIMARY classifier, Gemini parallel second opinion
- ROUTING_PROMPT classifies by INTENT not by language keywords
  → works in Portuguese, English, or any mixed language
- Portuguese keyword fallback added alongside English
- Gemini quota failure no longer kills routing
"""
import concurrent.futures
import logging

from ..models.gemini import ask_gemini

_log = logging.getLogger("classifier")

VALID_MODELS = {"GEMINI", "DEEPSEEK", "CLAUDE", "HAIKU", "N8N", "GITHUB", "SHELL"}

# ── Bilingual routing prompt — intent-based, language-agnostic ────────────────
ROUTING_PROMPT = """\
You are a request classifier for a multi-agent AI system.

The user may write in ANY language — English, Portuguese, Spanish, or mixed.
Classify by INTENT, not by the words used. Translate mentally if needed.

CATEGORIES:
- GITHUB → code, files, repos, commits, pull requests, website edits
             PT: código, ficheiro, repositório, commit, push, site
- N8N → workflows, automation, bots, scheduling, triggers, integrations
             PT: workflow, fluxo, automação, agendamento, integração, bot
- SHELL → terminal, commands, builds, deployments, server, APK, Flutter
             PT: terminal, comando, construir, servidor, implantação, apk
- DEEPSEEK → math, data, SQL, structured logic, calculations, spreadsheets
             PT: cálculo, dados, matemática, planilha, lógica, sql
- CLAUDE → writing, summarising, research, creative, long explanations
             PT: escrever, resumir, pesquisa, criativo, explicar, traduzir
- HAIKU → short questions, greetings, quick lookups, anything else

Request: {request}

Reply with ONLY one word: GITHUB, N8N, SHELL, DEEPSEEK, CLAUDE, or HAIKU.
No explanation. No punctuation. Just the category."""

# ── Bilingual keyword fallback — only when ALL models fail ────────────────────
_ALL_GITHUB = {
    # English
    "push", "commit", "pull request", "repository", "repo", "branch",
    "github", "merge", "clone", "git", "website", "html",
    # Portuguese
    "empurra", "empurrar", "commitar", "repositório", "repositorio",
    "ficheiro", "arquivo", "código", "site", "ramo",
}
_ALL_N8N = {
    # English
    "workflow", "automate", "automation", "schedule", "trigger",
    "n8n", "integration", "bot", "webhook", "cron",
    # Portuguese
    "fluxo", "automação", "automatizar", "agendamento",
    "agendar", "gatilho", "integração",
}
_ALL_SHELL = {
    # English
    "build", "deploy", "terminal", "shell", "command", "server",
    "apk", "flutter", "install", "restart", "run",
    # Portuguese
    "construir", "implantar", "implantação", "servidor",
    "comando", "instalar", "reiniciar", "executar", "rodar",
}
_ALL_DEEPSEEK = {
    # English
    "calculate", "math", "sql", "data", "algorithm", "formula",
    # Portuguese
    "calcular", "cálculo", "calculo", "matemática", "dados",
    "planilha", "fórmula", "formula", "lógica",
}
_ALL_CLAUDE = {
    # English
    "write", "draft", "summarize", "summary", "explain",
    "essay", "letter", "review", "translate", "analyze", "research",
    # Portuguese
    "escrever", "escreva", "redigir", "resumir", "resumo",
    "explicar", "carta", "traduzir", "tradução", "analisar", "pesquisar",
}


def _keyword_classify(request: str) -> str:
    """Bilingual keyword fallback — last resort when all model classifiers fail."""
    lower = request.lower()
    if any(k in lower for k in _ALL_GITHUB): return "GITHUB"
    if any(k in lower for k in _ALL_N8N): return "N8N"
    if any(k in lower for k in _ALL_SHELL): return "SHELL"
    if any(k in lower for k in _ALL_CLAUDE): return "CLAUDE"
    if any(k in lower for k in _ALL_DEEPSEEK): return "DEEPSEEK"
    return "HAIKU"


def _parse_model(raw: str) -> str | None:
    if not raw or raw.startswith("["):
        return None
    result = raw.strip().upper().split()[0].rstrip(".,;:")
    return result if result in VALID_MODELS else None


def _classify_with_haiku(request: str) -> str | None:
    try:
        from ..learning.internal_llm import ask_internal_fast
        raw = ask_internal_fast(ROUTING_PROMPT.format(request=request))
        return _parse_model(raw)
    except Exception as e:
        _log.debug("Haiku classifier failed: %s", e)
        return None


def _classify_with_gemini(request: str) -> str | None:
    try:
        raw = ask_gemini(ROUTING_PROMPT.format(request=request), system="")
        return _parse_model(raw)
    except Exception as e:
        _log.debug("Gemini classifier failed: %s", e)
        return None


def classify_request(request: str) -> str:
    """
    Classify a user request — language-agnostic, parallel, fault-tolerant.
    Works in English, Portuguese, or any language.
    Returns: GITHUB | N8N | SHELL | DEEPSEEK | CLAUDE | HAIKU
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_haiku = pool.submit(_classify_with_haiku, request)
        future_gemini = pool.submit(_classify_with_gemini, request)
        haiku_result = gemini_result = None
        try:
            haiku_result = future_haiku.result(timeout=8)
        except Exception:
            pass
        try:
            gemini_result = future_gemini.result(timeout=8)
        except Exception:
            pass

    if haiku_result and gemini_result:
        if haiku_result == gemini_result:
            return haiku_result
        return haiku_result # Haiku preferred on disagreement

    return haiku_result or gemini_result or _keyword_classify(request)



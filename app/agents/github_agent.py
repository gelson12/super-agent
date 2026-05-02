from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from ..config import settings
from .agent_planner import run_with_plan_and_recovery, extract_final_agent_text
from ..tools.github_tools import (
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
)
from ..tools.railway_tools import (
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
)
from ..tools.shell_tools import run_shell_command
from ..tools.obsidian_tools import OBSIDIAN_TOOLS
from ..tools.v0_tools import V0_TOOLS

_SYSTEM = """You are a GitHub assistant with LIVE access to Gelson's GitHub account (gelson12).

EXECUTION STANCE: Execute immediately. Never say 'I don't have access' — GITHUB_PAT is configured and live.

## SELF-HEALING — MANDATORY WHEN ERRORS OCCUR

If any GitHub tool returns an error (auth failure, rate limit, 404, network error):

1. AUTH FAILURES ("401 Bad credentials", "Bad PAT"):
   → Call railway_list_variables — confirm GITHUB_PAT is set in Railway environment
   → Call railway_get_logs — check if the PAT was recently rotated or expired
   → Report the exact variable state found; never just say "check your PAT"

2. RATE LIMIT ("403 rate limit exceeded"):
   → Call run_shell_command with "date" to get current UTC time
   → Report when the rate limit resets (GitHub rate limit resets every hour)
   → Queue the remaining tasks for retry

3. REPO/FILE NOT FOUND ("404"):
   → Call github_list_repos first to discover what repos actually exist
   → Try alternate branch names: main → master → develop
   → Never give up after a single 404 — adapt and retry

4. NETWORK/TIMEOUT errors:
   → Call railway_get_deployment_status to check if the container itself is healthy
   → Call railway_get_logs to see recent errors
   → Retry the operation once before escalating

NEVER tell the user to manually check GitHub, rotate a PAT, or go to any dashboard.
Use your tools to investigate first, fix what you can, and report exactly what you found.

## REPO DISCOVERY
If the user does not specify a repo name, call github_list_repos first to discover
all available repos, then choose the most relevant one based on the task context.

## KNOWN REPOS & WEBSITES
- **bridge-digital-solution.com** → lives in the `super-agent` repo under the `website/` directory
  - Main file: `website/index.html`
  - Instagram links appear at lines ~918 and ~1000 in that file
  - After editing, the Railway service `radiant-appreciation` will auto-redeploy from the push
- When the user says "the website" or "bridge-digital-solution.com", target `gelson12/super-agent`, path `website/index.html`

## WEBSITE MODIFICATION WORKFLOW
When asked to modify the website (HTML, links, icons, text):
1. Call `github_read_file(repo_name="super-agent", file_path="website/index.html")` first
2. Identify ALL occurrences of the target string (there are often 2 — header and footer)
3. Call `github_create_or_update_file` with the full updated content and a clear commit message
4. Confirm how many occurrences were updated

## WEBSITE DESIGN WITH v0.dev (V0_API_KEY IS CONFIGURED)
When asked to BUILD, CREATE, or DESIGN a new website or landing page from scratch:
1. Call `v0_generate_website(brief="<detailed brief>")` — this calls the v0.dev AI API and returns
   complete production-ready HTML+CSS+JS for the full page.
2. Commit the generated file to the correct repo path with `github_create_or_update_file`.
3. For adding NEW SECTIONS to an existing page, call `v0_generate_component(component_brief="...")` instead.

**V0_API_KEY IS SET** — you have full access to v0.dev's website generation API. Always use it
for new website creation tasks rather than writing HTML by hand. The API produces mobile-responsive,
SEO-optimised, accessible output far faster than manual coding.

Brief quality matters — always include: purpose, niche/industry, target location, colour scheme,
required sections, CTAs, and any integrations (call tracking, analytics, forms).

You can:
- List all repositories under gelson12 (use this when repo name is unknown)
- Read any file in any repo
- Create, update, or delete files (with a commit message)
- Create branches
- Open pull requests

Always confirm the exact action taken and its result. Use clear, descriptive commit messages.
When unsure of a branch name, try 'main' first then 'master'.
Never guess file content — read the file first if you need to modify it.

## VAULT WORKFLOW — DO THIS FOR EVERY NON-TRIVIAL TASK
FIRST (before acting):
  1. obsidian_search_vault("<task keywords>") → find prior repo work, commit patterns, file locations
  2. obsidian_read_note("GitHub/patterns.md") → check known repo structures and proven workflows

LAST (after a meaningful outcome):
  obsidian_append_to_note("KnowledgeBase/GitHub/outcomes.md",
    "## <date> <time> — OK/ERROR\n**Task:** <summary>\n**Repo:** <repo>\n**Action:** <action>\n**Result:** <result>")

Skip vault for simple read-only queries (list_repos, read_file with no follow-up write)."""

_GITHUB_TOOLS = [
    github_list_repos,
    github_list_files,
    github_read_file,
    github_create_or_update_file,
    github_delete_file,
    github_create_branch,
    github_create_pull_request,
    # Self-healing tools — used when GitHub errors occur
    railway_list_variables,
    railway_get_logs,
    railway_get_deployment_status,
    run_shell_command,
    # Obsidian knowledge vault — read/write notes, search prior context
    *OBSIDIAN_TOOLS,
    # v0.dev AI website generation — build full pages and components from a brief
    *V0_TOOLS,
]

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=settings.anthropic_api_key,
            max_tokens=settings.max_tokens_claude,
        )
        _agent = create_react_agent(llm, _GITHUB_TOOLS)
    return _agent


def _invoke(message: str) -> str:
    """Raw agent invoke via LangGraph + Anthropic API (last resort)."""
    try:
        agent = _get_agent()
        result = agent.invoke({
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": message},
            ]
        })
        text = extract_final_agent_text(result)
        return text or "[GitHub agent: no response]"
    except Exception as e:
        return f"[GitHub agent error: {str(e)[:200]}]"


def run_github_agent(message: str) -> str:
    """
    Run the GitHub agent.
    Routing via shared tiered_agent_invoke:
      - Informational → CLI → Gemini → Anthropic API (LangGraph) → DeepSeek (LangGraph)
      - Operational   → Anthropic API (LangGraph) → DeepSeek (LangGraph)
    """
    if not settings.github_pat:
        return "[GitHub agent error: GITHUB_PAT not set]"

    from .agent_routing import tiered_agent_invoke
    return tiered_agent_invoke(
        message=message,
        system_prompt=_SYSTEM,
        tools=_GITHUB_TOOLS,
        agent_type="github",
        source="github_agent",
    )

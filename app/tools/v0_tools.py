"""
v0.dev (Vercel AI) website / UI generation tool.

Allows the GitHub agent (Website Designer) to use the v0.dev API to generate
complete, production-ready website components from a natural-language brief.
The generated HTML/CSS/JS/React is then committed to GitHub via the existing
github_create_or_update_file tool.

Prerequisites
-------------
- Set the Railway env var V0_API_KEY (obtained from https://v0.dev → Settings → API Keys)

API reference
-------------
POST https://api.v0.dev/v1/chat
Headers: Authorization: Bearer <V0_API_KEY>, Content-Type: application/json
Body:    { "messages": [{ "role": "user", "content": "<prompt>" }] }
Response: { "text": "<generated_code_or_html>" }
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from langchain_core.tools import tool

from ..config import settings


def _v0_request(prompt: str, timeout: int = 90) -> str:
    """
    Send a generation prompt to v0.dev and return the generated code/HTML.
    Returns an error string (starting with '[') on failure — never raises.
    """
    api_key = settings.v0_api_key or os.environ.get("V0_API_KEY", "")
    if not api_key:
        return "[v0_tools error: V0_API_KEY not configured — set it as a Railway env var]"

    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.v0.dev/v1/chat",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        text = data.get("text") or data.get("content") or ""
        if not text:
            return f"[v0_tools error: empty response from v0.dev — raw: {str(data)[:200]}]"
        return text.strip()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        return f"[v0_tools HTTP {e.code}: {body}]"
    except urllib.error.URLError as e:
        return f"[v0_tools network error: {e.reason}]"
    except (json.JSONDecodeError, KeyError) as e:
        return f"[v0_tools parse error: {e}]"
    except Exception as e:
        return f"[v0_tools unexpected error: {e}]"


@tool
def v0_generate_website(brief: str) -> str:
    """
    Generate a complete, production-ready website (HTML/CSS/JS) using the v0.dev API.

    Use this tool when asked to BUILD, DESIGN, or CREATE a new website or landing page.
    Provide a detailed brief including: purpose, niche, location, tone, key sections,
    colour palette preferences, and any tracking integrations (e.g. call tracking).

    Returns the full generated HTML/CSS/JS source code ready to be committed to GitHub.

    Example brief:
      "Single-page plumbing landing page for Birmingham, UK. Sections: hero with CTA,
       services grid (drain unblocking, boiler repair, emergency), testimonials, contact
       form. Blue/white colour scheme. Embed call tracking script for +44 121 XXX XXXX."
    """
    if not brief or len(brief.strip()) < 10:
        return "[v0_tools error: brief is too short — provide a detailed description]"

    structured_prompt = (
        "You are an expert web designer. Generate a complete, production-ready single-file "
        "website (HTML + embedded CSS + minimal vanilla JS, no external build step required). "
        "The website must be:\n"
        "- Mobile-responsive (CSS grid/flexbox)\n"
        "- SEO-friendly (semantic HTML5 tags, meta description, og:image)\n"
        "- Fast-loading (inline critical CSS, lazy-load images)\n"
        "- Accessible (ARIA labels, sufficient colour contrast)\n\n"
        f"Brief:\n{brief}\n\n"
        "Output ONLY the raw HTML file content. No explanations, no markdown fences."
    )
    return _v0_request(structured_prompt)


@tool
def v0_generate_component(component_brief: str) -> str:
    """
    Generate a single website component (hero section, pricing table, contact form, etc.)
    using the v0.dev API. Useful for adding sections to an existing page.

    Returns raw HTML/CSS for the component, ready to be spliced into an existing page.
    """
    if not component_brief or len(component_brief.strip()) < 5:
        return "[v0_tools error: component_brief is too short]"

    prompt = (
        "Generate a single, standalone HTML+CSS component (no full page boilerplate). "
        "Output only the component markup and its scoped <style> block. "
        "Make it mobile-responsive, accessible, and production-ready.\n\n"
        f"Component request: {component_brief}"
    )
    return _v0_request(prompt)


V0_TOOLS = [v0_generate_website, v0_generate_component]

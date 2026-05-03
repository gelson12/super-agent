"""
Document generation tools — produce PDF or plain-text contracts / proposals.

Uses only stdlib (no weasyprint/reportlab dependency) via a minimal HTML→PDF
approach: if the PDFKIT_WKHTMLTOPDF env var points to wkhtmltopdf, we use it;
otherwise we fall back to returning clean HTML that can be opened in any browser
and saved as PDF. The GitHub agent commits the output file; the client gets the
raw GitHub URL or a Vercel-deployed preview.

Tools
-----
  generate_proposal(client_name, service, price_gbp, notes) → HTML string
  generate_contract(client_name, service, price_gbp, start_date, notes) → HTML string
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone

from langchain_core.tools import tool

_CSS = """
body{font-family:Georgia,serif;max-width:860px;margin:48px auto;color:#1a1a1a;line-height:1.7}
h1{font-size:2rem;border-bottom:3px solid #0a2540;padding-bottom:8px;margin-bottom:4px}
h2{font-size:1.2rem;color:#0a2540;margin-top:2rem}
.meta{color:#555;font-size:.9rem;margin-bottom:2rem}
.section{margin:1.5rem 0}
.price-box{background:#f4f8ff;border:1px solid #c8d8f0;border-radius:8px;
  padding:1rem 1.5rem;margin:1.5rem 0;font-size:1.1rem}
.price-box strong{font-size:1.4rem;color:#0a2540}
.sig-block{margin-top:4rem;display:flex;gap:4rem}
.sig-line{border-top:1px solid #333;padding-top:4px;min-width:220px;color:#555;font-size:.85rem}
footer{margin-top:3rem;font-size:.8rem;color:#888;text-align:center}
"""

_COMPANY = "Bridge Digital Solution"
_COMPANY_EMAIL = "bridge.digital.solution@gmail.com"
_COMPANY_WEBSITE = "bridge-digital-solution.com"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%d %B %Y")


def _html_wrapper(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
<footer>{_COMPANY} · {_COMPANY_EMAIL} · {_COMPANY_WEBSITE}</footer>
</body>
</html>"""


def _try_pdf(html: str, out_path: str) -> bool:
    """Try converting HTML to PDF via wkhtmltopdf if available. Returns True on success."""
    wk = os.environ.get("PDFKIT_WKHTMLTOPDF", "")
    if not wk:
        for candidate in ["/usr/bin/wkhtmltopdf", "/usr/local/bin/wkhtmltopdf"]:
            if os.path.isfile(candidate):
                wk = candidate
                break
    if not wk:
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            f.write(html.encode("utf-8"))
            tmp_html = f.name
        result = subprocess.run(
            [wk, "--quiet", tmp_html, out_path],
            timeout=30,
            capture_output=True,
        )
        os.unlink(tmp_html)
        return result.returncode == 0 and os.path.isfile(out_path)
    except Exception:
        return False


@tool
def generate_proposal(
    client_name: str,
    service: str,
    price_gbp: float,
    notes: str = "",
    output_path: str = "",
) -> str:
    """
    Generate a professional project proposal document (HTML, optionally PDF).

    Creates a ready-to-send proposal for a client quoting the specified service and price.
    The output is complete HTML you can commit to GitHub, deploy on Vercel, or email directly.

    Args:
        client_name:  Full name or business name (e.g. "Smith Plumbing Ltd").
        service:      Description of the service being proposed
                      (e.g. "5-page website with booking form and Google Maps integration").
        price_gbp:    Total project price in GBP (e.g. 850.0).
        notes:        Optional extra terms, timeline, or project notes.
        output_path:  Optional file path to save the HTML/PDF.
                      If empty, returns the HTML content directly.

    Returns:
        HTML content of the proposal, or "[doc_tools error: ...]" on failure.
    """
    if not client_name.strip() or not service.strip():
        return "[doc_tools error: client_name and service are required]"

    price_str = f"£{price_gbp:,.2f}"
    date = _today()

    notes_html = f"<div class='section'><h2>Additional Notes</h2><p>{notes}</p></div>" if notes.strip() else ""

    body = f"""
<h1>Project Proposal</h1>
<div class="meta">Prepared for: <strong>{client_name}</strong> &nbsp;·&nbsp; Date: {date}</div>

<div class="section">
<h2>Prepared By</h2>
<p><strong>{_COMPANY}</strong><br>
{_COMPANY_WEBSITE} &nbsp;·&nbsp; {_COMPANY_EMAIL}</p>
</div>

<div class="section">
<h2>Scope of Work</h2>
<p>{service}</p>
</div>

<div class="price-box">
Project Investment: <strong>{price_str}</strong><br>
<span style="font-size:.9rem;color:#555">All prices inclusive of VAT where applicable</span>
</div>

<div class="section">
<h2>What's Included</h2>
<ul>
  <li>Discovery &amp; requirements scoping</li>
  <li>Design, development, and testing</li>
  <li>1 round of revisions</li>
  <li>Deployment and go-live support</li>
  <li>30 days post-launch support</li>
</ul>
</div>

{notes_html}

<div class="section">
<h2>Next Steps</h2>
<p>To proceed, please confirm acceptance of this proposal and arrange the deposit payment.
A 50% deposit (£{price_gbp/2:,.2f}) is required before work begins, with the remainder
due upon project completion.</p>
</div>

<div class="sig-block">
  <div>
    <div class="sig-line">Client signature / date</div>
  </div>
  <div>
    <div class="sig-line">{_COMPANY} / date</div>
  </div>
</div>
"""
    html = _html_wrapper(f"Proposal — {client_name}", body)

    if output_path:
        if output_path.endswith(".pdf") and _try_pdf(html, output_path):
            return f"PDF saved to {output_path}"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        return f"HTML saved to {output_path}"

    return html


@tool
def generate_contract(
    client_name: str,
    service: str,
    price_gbp: float,
    start_date: str = "",
    payment_terms: str = "50% deposit upfront, 50% on completion",
    notes: str = "",
    output_path: str = "",
) -> str:
    """
    Generate a legally structured service contract between Bridge Digital Solution and a client.

    Creates a standard freelance/agency contract covering scope, payment, IP, and liability.
    Output is complete HTML — commit to GitHub and share the raw link, or deploy on Vercel.

    Args:
        client_name:    Full legal name or company name of the client.
        service:        Detailed description of deliverables.
        price_gbp:      Total contract value in GBP.
        start_date:     Anticipated project start date (e.g. "12 May 2026"). Defaults to today.
        payment_terms:  Payment schedule. Default: "50% deposit upfront, 50% on completion".
        notes:          Optional additional clauses or special conditions.
        output_path:    Optional file path to save HTML/PDF.

    Returns:
        HTML contract content, or "[doc_tools error: ...]" on failure.
    """
    if not client_name.strip() or not service.strip():
        return "[doc_tools error: client_name and service are required]"

    price_str = f"£{price_gbp:,.2f}"
    date = _today()
    if not start_date.strip():
        start_date = date

    notes_html = (
        f"<div class='section'><h2>Special Conditions</h2><p>{notes}</p></div>"
        if notes.strip() else ""
    )

    body = f"""
<h1>Service Agreement</h1>
<div class="meta">Date: {date} &nbsp;·&nbsp; Ref: BDS-{datetime.now().strftime('%Y%m%d%H%M')}</div>

<div class="section">
<h2>Parties</h2>
<p><strong>Service Provider:</strong> {_COMPANY}, {_COMPANY_WEBSITE}<br>
<strong>Client:</strong> {client_name}</p>
</div>

<div class="section">
<h2>1. Scope of Services</h2>
<p>{service}</p>
<p>Start date: <strong>{start_date}</strong></p>
</div>

<div class="price-box">
Contract Value: <strong>{price_str}</strong><br>
Payment Terms: {payment_terms}
</div>

<div class="section">
<h2>2. Deliverables &amp; Acceptance</h2>
<p>The Service Provider will deliver the agreed work to a professional standard.
The Client has <strong>5 business days</strong> to raise any defects in writing after delivery.
Silence after 5 days constitutes acceptance.</p>
</div>

<div class="section">
<h2>3. Intellectual Property</h2>
<p>Upon receipt of full payment, all intellectual property rights in the deliverables
transfer to the Client. The Service Provider retains the right to display the work
in their portfolio unless otherwise agreed in writing.</p>
</div>

<div class="section">
<h2>4. Revisions</h2>
<p>This agreement includes one round of reasonable revisions. Additional change requests
outside the agreed scope will be quoted separately at the Service Provider's standard rate.</p>
</div>

<div class="section">
<h2>5. Liability</h2>
<p>The Service Provider's total liability under this agreement shall not exceed the
total fees paid. The Service Provider is not liable for indirect or consequential loss.</p>
</div>

<div class="section">
<h2>6. Cancellation</h2>
<p>Either party may terminate this agreement with 7 days' written notice. The Client
remains liable for all work completed up to the termination date. Deposits are
non-refundable once work has commenced.</p>
</div>

<div class="section">
<h2>7. Governing Law</h2>
<p>This agreement is governed by the laws of England and Wales.</p>
</div>

{notes_html}

<div class="sig-block">
  <div>
    <div class="sig-line">Client signature &amp; date</div>
  </div>
  <div>
    <div class="sig-line">{_COMPANY} signature &amp; date</div>
  </div>
</div>
"""
    html = _html_wrapper(f"Service Agreement — {client_name}", body)

    if output_path:
        if output_path.endswith(".pdf") and _try_pdf(html, output_path):
            return f"PDF saved to {output_path}"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        return f"HTML saved to {output_path}"

    return html


DOCUMENT_TOOLS = [generate_proposal, generate_contract]

"""
CV Builder — PDF rendering. Pure Jinja2 + WeasyPrint, no LLM/network calls.

Deliberately does NOT contain any environment/DYLD_FALLBACK_LIBRARY_PATH fixup
logic itself (unlike cv_builder.py's CLI entrypoint). This module is imported by
both the standalone CLI and, eventually, Charlie's own long-running Telegram bot
process — a self-re-exec fixup here would be safe for the former but would
silently restart the entire bot process for the latter the first time a CV is
generated, an undocumented and surprising side effect. See requirements.txt's
weasyprint comment and cv_builder.py for the real fix. If WeasyPrint's import
below fails, that's the caller's environment to fix, not this module's job to
paper over.
"""
from pathlib import Path

import jinja2
import pypdf
import weasyprint

TEMPLATE_DIR = Path(__file__).parent


def render_cv_pdf(tailored_cv: dict, profile: dict, output_path: Path) -> dict:
    """
    Renders tailored_cv + profile through template.html to a PDF at output_path.

    Returns {"path": Path, "pages": int, "notes": [str, ...]}. A rendered page
    count other than 1 is NOT an error — the model's bullet-count budget is a
    heuristic, not a measurement of actual rendered height — but it is always
    surfaced via "notes" so a caller never silently hands over a multi-page
    "one-page CV". A true auto-shrink-and-retry loop is deliberately out of
    scope for v1.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    html = template.render(tailored_cv=tailored_cv, profile=profile)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(str(output_path))

    reader = pypdf.PdfReader(str(output_path))
    pages = len(reader.pages)

    notes = []
    if pages != 1:
        notes.append(
            f"Rendered as {pages} page{'s' if pages != 1 else ''}, not 1 — "
            "the selected roles/bullets need tightening for this JD."
        )

    return {"path": output_path, "pages": pages, "notes": notes}

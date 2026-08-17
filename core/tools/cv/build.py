"""
CV Builder — shared orchestrator. Both the standalone CLI (cv_builder.py) and
Charlie's Telegram tool call the same build_cv_for_jd() — not tool-calling-tool,
two interfaces on one plain-Python function.

build_cv_for_jd is the one async function in this package: it wraps the whole
sync chain (repository load -> draft -> resolve -> render) in a single
asyncio.to_thread call, matching core/scheduler.py's _run_grants_pipeline
precedent (BUG-019) rather than making every sync step async individually.
"""
import asyncio
import re
from datetime import date
from pathlib import Path

from .generate import DEFAULT_BULLET_TARGET, draft_tailored_cv
from .render import render_cv_pdf
from .repository import load_cv_repository
from .review import review_cv

OUTPUT_DIR = Path(__file__).parent.parent.parent.parent / "cv" / "output"
MAX_TRIM_STEPS = 15
MAX_REVIEW_ROUNDS = 3

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _format_date(value: str | None) -> str:
    """'2020-11' -> 'November 2020'. Falls back to the raw value if unparseable."""
    if not value:
        return "Present"
    m = re.match(r"^(\d{4})-(\d{2})$", value)
    if not m:
        return value
    year, month = m.group(1), int(m.group(2))
    if not (1 <= month <= 12):
        return value
    return f"{_MONTH_NAMES[month]} {year}"


def _date_range(role_period: dict) -> str:
    return f"{_format_date(role_period.get('start_date'))} – {_format_date(role_period.get('end_date'))}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "cv"


def _resolve_role(draft_role: dict, companies: dict) -> dict | None:
    """
    Maps generate.py's compact {company_id, bullets, sub_engagement_ids,
    sub_engagement_bullets} into render.py's presentation-ready role dict, by
    looking up company details from the repository. Returns None (and the
    caller skips it, with a note) if the model names a company_id that
    doesn't exist, or if that company has no usable role_periods (a company
    file mid-fill-out could plausibly have an empty list) — either way a
    single bad/incomplete entry shouldn't crash or fail the whole draft.

    Sub-engagement bullets are flattened into the same flat bullet list as the
    parent's own bullets — matching Jonathan's real CVs, which describe TS
    Group's portfolio companies (MyMonero, GloBee, etc.) as ordinary bullets
    naming the sub-engagement inline, not as a separate visual sub-block. An
    earlier version of this template rendered sub-engagements as their own
    indented, bordered mini-entries; Jonathan reviewed real output and said it
    looked nothing like his actual CVs, so this was reverted.
    """
    cid = draft_role.get("company_id")
    company = companies.get(cid)
    if company is None:
        return None

    role_periods = company.get("role_periods") or []
    if not role_periods:
        return None
    role_period = role_periods[0]
    bullets = list(draft_role.get("bullets", []))
    for sub_id in draft_role.get("sub_engagement_ids") or []:
        sub = companies.get(sub_id)
        if sub is None or sub.get("parent") != cid:
            continue
        bullets.extend((draft_role.get("sub_engagement_bullets") or {}).get(sub_id, []))

    variants = role_period.get("title_variants", [])
    valid_values = {v["value"] for v in variants}
    role_title = draft_role.get("role_title")
    if role_title not in valid_values:
        # Model omitted it, or returned something not in the offered list — fall
        # back to the first variant rather than trust free text for this field.
        role_title = variants[0]["value"] if variants else ""

    return {
        "company_id": cid,
        "company_name": company["name"],
        "location": company.get("location"),
        "role_title": role_title,
        "date_range": _date_range(role_period),
        # Frozen per Jonathan (2026-08-11): always the repository's static text,
        # never model-drafted — company descriptions are agreed on directly with
        # Jonathan rather than reworded per JD. See ts-group.md/seedify.md's
        # "resolved" logs for the reversal of the earlier per-JD-dynamic approach.
        "company_description": (company.get("company_description") or "").strip(),
        "bullets": bullets,
    }


def _assemble_tailored_cv(draft: dict, repository: dict) -> tuple:
    """
    Shared by the initial draft and every revision round: resolves draft_cv's
    compact role list against the repository and returns (tailored_cv, notes).
    tailored_cv["roles"] is [] if nothing could be resolved — callers must
    check for that (an empty-roles draft is treated as a failed round, not a
    valid empty CV).
    """
    companies = repository["companies"]
    notes = list(draft.get("notes", []))
    roles = []
    for draft_role in draft.get("roles", []):
        resolved = _resolve_role(draft_role, companies)
        if resolved is None:
            cid = draft_role.get("company_id")
            reason = "an unknown company id" if cid not in companies else "a company with no usable role_periods"
            notes.append(f"Model referenced '{cid}', {reason} — skipped.")
            continue
        roles.append(resolved)

    interests = repository["profile"].get("interests", []) if draft.get("include_interests") else []
    tailored_cv = {"summary": draft.get("summary", ""), "roles": roles, "interests": interests}
    return tailored_cv, notes


def _tailored_cv_as_text(tailored_cv: dict) -> str:
    """
    Plain-text rendition of a drafted CV for the reviewer to read — deliberately
    not a re-render through render.py/WeasyPrint, since the reviewer only needs
    to judge content (duplication, phrasing, JD fit), not visual layout, and a
    real PDF render would cost significantly more per review round for no
    benefit to that judgment.

    Deliberately mirrors template.html's actual structure with no field-name
    labels (no "Title:"/"Description:") — an earlier version added those
    labels for the reviewer's own readability, but the real template never
    renders them, and the reviewer repeatedly (and correctly, given what it
    was shown) flagged them as bad CV formatting that doesn't actually exist
    in the shipped output. Keep this in sync with template.html's role block.
    """
    lines = []
    if tailored_cv.get("summary"):
        lines.append(f"SUMMARY: {tailored_cv['summary']}\n")
    for role in tailored_cv.get("roles", []):
        location = f", {role['location']}" if role.get("location") else ""
        lines.append(f"{role['company_name']}{location} ({role['date_range']})")
        lines.append(role["role_title"])
        if role.get("company_description"):
            lines.append(role["company_description"])
        for bullet in role.get("bullets", []):
            lines.append(f"- {bullet}")
        lines.append("")
    if tailored_cv.get("interests"):
        lines.append(f"Interests: {', '.join(tailored_cv['interests'])}")
    return "\n".join(lines)


_EM_DASH = "—"


def _em_dash_findings(cv_text: str) -> list:
    """
    Deterministic em-dash check, run in code rather than asked of the LLM
    reviewer. Live testing showed the model couldn't reliably self-report on
    this even when explicitly instructed: across several real runs it
    repeatedly wrote out "let me check for an em dash... on re-reading there
    isn't one" and then still submitted that reasoning as a finding anyway
    (contradicting its own conclusion), which inflated the finding count and
    blocked the loop from ever converging on an otherwise-clean draft. A
    plain string search is both more reliable and cheaper for a check this
    mechanical — reserve the LLM reviewer for judgment calls (duplication,
    readability, JD fit) that actually need language understanding.
    """
    return [
        {"category": "ai_tell_phrasing", "issue": f"Em dash (—) found: '{line.strip()}'"}
        for line in cv_text.splitlines()
        if _EM_DASH in line
    ]


def _trim_to_one_page(tailored_cv: dict, profile: dict, output_path: Path) -> tuple:
    """Runs render_cv_pdf, then the deterministic trim loop, until it fits one page or gives up. Returns the final render_result and mutates tailored_cv in place."""
    render_result = render_cv_pdf(tailored_cv, profile, output_path)
    trims = 0
    while render_result["pages"] != 1 and trims < MAX_TRIM_STEPS and _trim_one_step(tailored_cv):
        trims += 1
        render_result = render_cv_pdf(tailored_cv, profile, output_path)
    return render_result, trims


def _trim_one_step(tailored_cv: dict) -> bool:
    """
    Drops the last bullet of the last role, or the whole last role if it only
    has one bullet left. Mutates tailored_cv["roles"] in place. Returns False
    once there's nothing left to cut (down to one role with one bullet) so the
    caller knows to stop.

    Roles are ordered most-recent-first (generate.py's own instruction), so
    "last" is the oldest included role — cutting from there first matches
    ordinary resume-trimming judgment (recent/relevant experience stays, older
    experience gets compressed first), not an arbitrary choice.
    """
    roles = tailored_cv["roles"]
    if not roles:
        return False
    last = roles[-1]
    if len(last["bullets"]) > 1:
        last["bullets"].pop()
        return True
    if len(roles) > 1:
        roles.pop()
        return True
    return False


def _build_cv_for_jd_sync(jd_text: str, role_hint: str) -> dict:
    """
    Draft -> trim-to-one-page -> quality review/revise loop, capped at
    MAX_REVIEW_ROUNDS. The trim loop (drop the oldest role's last bullet, then
    whole roles, re-render, repeat) is deterministic, not a second LLM call —
    the model can't see actual rendered height, so its own budget-following is
    inconsistent call to call (observed directly against real renders).

    The review loop is a genuinely independent model call (review.py's
    review_cv) reading the finished draft fresh, not the same conversation as
    the drafter, plus a deterministic em-dash check (_em_dash_findings) that
    doesn't rely on the model self-reporting on something a plain string
    search does more reliably — it flags issues (AI-tell phrasing,
    duplication, readability, JD fit, formatting consistency, factual
    overreach); the drafter then either fixes each one or pushes back with a
    reason (recorded in notes, not silently dropped) via generate.py's
    revision-mode draft_tailored_cv call. The loop always reviews before
    deciding whether to revise again, so whatever ships has always been
    through at least one review pass — capped at MAX_REVIEW_ROUNDS revisions
    (not review calls) rather than run to full convergence, since an
    unbounded loop against a live API is an unbounded cost/latency risk; if it
    hasn't converged by then, the last-reviewed draft ships with the
    outstanding findings surfaced in notes rather than looping forever.
    """
    repository = load_cv_repository()
    profile = repository["profile"]
    draft = draft_tailored_cv(jd_text, repository, bullet_target=DEFAULT_BULLET_TARGET)

    if "error" in draft:
        return {"pdf_path": None, "summary": None, "notes": [draft["error"]]}

    tailored_cv, notes = _assemble_tailored_cv(draft, repository)
    if not tailored_cv["roles"]:
        notes.append("No roles could be resolved from the draft — nothing to render.")
        return {"pdf_path": None, "summary": None, "notes": notes}

    slug = _slugify(role_hint or tailored_cv["roles"][0]["company_name"])
    output_path = OUTPUT_DIR / f"{slug}-{date.today().isoformat()}.pdf"

    render_result, trims = _trim_to_one_page(tailored_cv, profile, output_path)
    if trims:
        notes.append(f"Automatically trimmed {trims} bullet(s)/role(s) (oldest first) to fit one page.")

    # revisions_done counts completed revisions (0 after the initial draft).
    # The loop always reviews the CURRENT tailored_cv first, and only revises
    # again if there's still budget — so whatever ships (the initial draft, or
    # the Nth revision) has always been through at least one review pass
    # itself. An earlier version capped on revisions alone, which let the
    # final, MAX_REVIEW_ROUNDS-th revision ship completely unreviewed —
    # exactly the output that mattered most. This costs one extra review call
    # in the worst case (MAX_REVIEW_ROUNDS + 1 reviews for MAX_REVIEW_ROUNDS
    # revisions) but that's the price of never shipping unreviewed content.
    revisions_done = 0
    approved = False
    while True:
        cv_text = _tailored_cv_as_text(tailored_cv)
        em_dash_findings = _em_dash_findings(cv_text)
        review = review_cv(jd_text, cv_text)
        if "error" in review:
            if em_dash_findings:
                notes.append(
                    f"Quality review step failed ({review['error']}), but "
                    f"{len(em_dash_findings)} deterministic em-dash issue(s) were still "
                    "detected in-process and are being fixed before shipping: "
                    + "; ".join(f["issue"] for f in em_dash_findings)
                )
                em_dash_fix = draft_tailored_cv(
                    jd_text, repository, bullet_target=DEFAULT_BULLET_TARGET,
                    prior_draft=draft, reviewer_findings=em_dash_findings,
                )
                if "error" not in em_dash_fix:
                    fixed_cv, fix_notes = _assemble_tailored_cv(em_dash_fix, repository)
                    if fixed_cv["roles"]:
                        draft = em_dash_fix
                        tailored_cv = fixed_cv
                        notes.extend(fix_notes)
                        render_result, retrims = _trim_to_one_page(tailored_cv, profile, output_path)
                        if retrims:
                            notes.append(f"Re-trimmed {retrims} bullet(s)/role(s) after the em-dash fix to keep one page.")
            else:
                notes.append(f"Quality review step failed ({review['error']}) — proceeding with the current draft.")
            break

        # Non-empty findings always mean "not approved" regardless of the
        # model's own approved flag — a reviewer returning approved=true
        # alongside real findings is a self-contradictory response, and
        # trusting the flag over the findings would silently drop them.
        findings = em_dash_findings + (review.get("findings") or [])
        if not findings:
            approved = True
            break

        notes.append(
            f"Review after {revisions_done} revision(s) found {len(findings)} issue(s): "
            + "; ".join(f"[{f.get('category', 'other')}] {f.get('issue', '')}" for f in findings)
        )

        if revisions_done >= MAX_REVIEW_ROUNDS:
            notes.append(
                f"Quality review did not fully converge after {MAX_REVIEW_ROUNDS} revision "
                "round(s) — shipping the last-reviewed draft with the findings above still "
                "outstanding rather than revising further."
            )
            break

        revised_draft = draft_tailored_cv(
            jd_text, repository, bullet_target=DEFAULT_BULLET_TARGET,
            prior_draft=draft, reviewer_findings=findings,
        )
        if "error" in revised_draft:
            notes.append(f"Revision failed ({revised_draft['error']}) — keeping the prior, already-reviewed draft.")
            break

        revised_cv, revise_notes = _assemble_tailored_cv(revised_draft, repository)
        if not revised_cv["roles"]:
            notes.append("Revision produced no resolvable roles — keeping the prior, already-reviewed draft.")
            break

        draft = revised_draft
        tailored_cv = revised_cv
        revisions_done += 1
        notes.extend(revise_notes)
        render_result, retrims = _trim_to_one_page(tailored_cv, profile, output_path)
        if retrims:
            notes.append(f"Re-trimmed {retrims} bullet(s)/role(s) after revision {revisions_done} to keep one page.")

    if approved and revisions_done:
        notes.append(f"Quality review approved after {revisions_done} revision round(s).")
    elif approved:
        notes.append("Quality review approved on first pass — no revisions needed.")

    if tailored_cv["interests"]:
        notes.append("Included an Interests section — the model judged this JD values well-roundedness/culture fit.")

    notes.extend(render_result["notes"])

    return {"pdf_path": render_result["path"], "summary": tailored_cv["summary"], "notes": notes}


async def build_cv_for_jd(jd_text: str, role_hint: str = "") -> dict:
    """
    Returns {"pdf_path": Path | None, "summary": str | None, "notes": [str, ...]}.
    pdf_path is None only on failure (drafting failed, or nothing could be
    resolved/rendered) — callers must check for that before assuming success.
    """
    return await asyncio.to_thread(_build_cv_for_jd_sync, jd_text, role_hint)


def _build_maximal_cv_sync() -> dict:
    """
    Renders every block from every company verbatim, ignoring the one-page
    budget — the transcription-accuracy check flagged as a next step in
    cv/ingestion-notes.md: skim this side-by-side against the original source
    documents for typos/wrong figures, as a check distinct from resolving
    genuine source-document disagreements (which ingestion-notes.md already
    covers). No LLM call and no JD input — this is a raw, deterministic dump
    of the repository, not a tailored draft, so canonical_text is used
    unmodified and nothing is selected or trimmed.

    Each bullet is prefixed with its block id (and, for sub-engagement
    blocks, "child_id / block_id") so a spotted error can be traced straight
    back to the source block in cv/companies/ for correction.
    """
    repository = load_cv_repository()
    companies = repository["companies"]

    top_level = [
        (cid, c) for cid, c in companies.items() if not c.get("parent")
    ]
    top_level.sort(
        # `or [{}]` (not a .get default) so a company with role_periods present
        # but an empty list — not just a missing key — still falls back safely.
        key=lambda item: (item[1].get("role_periods") or [{}])[0].get("start_date", ""),
        reverse=True,
    )

    roles = []
    for cid, company in top_level:
        role_periods = company.get("role_periods", [])
        role_period = role_periods[0] if role_periods else {}
        variants = role_period.get("title_variants", [])
        role_title = " / ".join(v["value"] for v in variants)

        bullets = [
            f"[{b['id']}] {b['canonical_text'].strip()}"
            for b in company.get("blocks", [])
        ]
        for child_id in company.get("children", []):
            child = companies[child_id]
            for b in child.get("blocks", []):
                bullets.append(f"[{child_id} / {b['id']}] {b['canonical_text'].strip()}")

        roles.append({
            "company_id": cid,
            "company_name": company["name"],
            "location": company.get("location"),
            "role_title": role_title,
            "date_range": _date_range(role_period) if role_period else "",
            "company_description": (company.get("company_description") or "").strip(),
            "bullets": bullets,
        })

    profile = repository["profile"]
    tailored_cv = {
        "summary": (profile.get("neutral_summary") or "").strip(),
        "roles": roles,
        "interests": profile.get("interests", []),
    }
    output_path = OUTPUT_DIR / f"maximal-transcription-check-{date.today().isoformat()}.pdf"

    render_result = render_cv_pdf(tailored_cv, profile, output_path)
    notes = [
        f"Maximal CV rendered as {render_result['pages']} page(s) — multi-page is "
        "expected and correct here, since the one-page budget is deliberately "
        "ignored for this check."
    ]

    return {"pdf_path": render_result["path"], "summary": tailored_cv["summary"], "notes": notes}


async def build_maximal_cv() -> dict:
    """
    Returns {"pdf_path": Path, "summary": str, "notes": [str, ...]} — see
    _build_maximal_cv_sync for what this renders and why.
    """
    return await asyncio.to_thread(_build_maximal_cv_sync)

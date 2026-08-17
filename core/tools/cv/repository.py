"""
CV Builder — repository loader. Parses cv/ (gitignored, personal work-history
data — see data-architecture.md) into one structured dict: profile + companies
(including nested sub-engagements, e.g. TS Group's portfolio) with each
company's tagged experience blocks. Pure, no network/LLM calls.
"""
from pathlib import Path

import yaml

CV_ROOT = Path(__file__).parent.parent.parent.parent / "cv"


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing opening '---' frontmatter delimiter")
    end = text.index("\n---", 4)
    return yaml.safe_load(text[4:end]) or {}


def _normalize_role_periods(role_periods: list) -> None:
    """
    Mutates each role_period in place so it always has "title_variants" — a
    list of {"value", "tags"} dicts — regardless of whether the source file
    wrote a single "title" string (the common case) or multiple tagged
    "title_variants" (for a role whose framing genuinely depends on the JD —
    e.g. TS Group's actual title was "General Counsel," with "Operating
    Advisor" describing the broader scope of the work rather than a second
    formal title; which to lead with depends on whether the JD is legal- or
    ops-focused, a real distinction Jonathan drew, not just wording noise).
    Downstream code only ever reads "title_variants".
    """
    for rp in role_periods:
        if "title_variants" in rp:
            continue
        rp["title_variants"] = [{"value": rp.get("title", ""), "tags": []}]


def load_cv_repository(cv_root: Path = CV_ROOT) -> dict:
    """
    Returns {"profile": {...}, "companies": {id: company_dict}}.

    Each company_dict is that file's frontmatter plus two additions: "_source"
    (the file's path relative to cv_root, for error messages) and "children"
    (ids of any other company whose "parent" field points at this one — e.g.
    TS Group's seven sub-engagements). "children" is derived here so callers
    don't have to re-derive the parent/child structure themselves.
    """
    profile_path = cv_root / "profile.yaml"
    profile = yaml.safe_load(profile_path.read_text()) if profile_path.exists() else {}

    companies = {}
    for md_path in sorted(cv_root.glob("companies/**/*.md")):
        data = _parse_frontmatter(md_path)
        cid = data.get("id")
        if not cid:
            raise ValueError(f"{md_path}: missing required 'id' field")
        if cid in companies:
            raise ValueError(
                f"Duplicate company id '{cid}' in {md_path} "
                f"(already defined in {companies[cid]['_source']})"
            )
        _normalize_role_periods(data.get("role_periods", []))
        data["_source"] = str(md_path.relative_to(cv_root))
        data["children"] = []
        companies[cid] = data

    for cid, data in companies.items():
        parent = data.get("parent")
        if parent:
            if parent not in companies:
                raise ValueError(f"{data['_source']}: parent '{parent}' not found among loaded companies")
            companies[parent]["children"].append(cid)

    return {"profile": profile, "companies": companies}

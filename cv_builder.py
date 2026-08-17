#!/usr/bin/env python3
"""
CV Builder — standalone CLI. Run directly from the terminal, no Telegram needed:

    source venv/bin/activate
    python cv_builder.py --jd-file path/to/jd.txt [--role-hint "Some Role"]
    python cv_builder.py --jd-text "paste inline" [--role-hint "Some Role"]
    python cv_builder.py --maximal

The DYLD_FALLBACK_LIBRARY_PATH fixup below MUST run before anything imports
weasyprint (transitively, via core.tools.cv.build/render) — see
requirements.txt's weasyprint comment and core/tools/cv/render.py's docstring
for the full story. Safe to self-re-exec here specifically because this is a
short-lived script restarting itself once at launch — this same trick must NOT
be used inside Charlie's long-running Telegram bot process (see render.py).
"""
import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path


def _homebrew_lib_dirs() -> list:
    candidates = []
    try:
        result = subprocess.run(["brew", "--prefix"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            candidates.append(result.stdout.strip())
    except Exception:
        pass
    candidates += ["/opt/homebrew", "/usr/local"]
    dirs = []
    for c in candidates:
        lib = f"{c}/lib"
        if lib not in dirs and os.path.isdir(lib):
            dirs.append(lib)
    return dirs


def _ensure_weasyprint_env():
    if os.environ.get("_CV_DYLD_PATCHED") == "1":
        return
    lib_dirs = _homebrew_lib_dirs()
    if not lib_dirs:
        return  # nothing we can do; let the real import error surface naturally
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))
    os.environ["_CV_DYLD_PATCHED"] = "1"
    os.execve(sys.executable, [sys.executable] + sys.argv, os.environ)


_ensure_weasyprint_env()

from core.tools.cv.build import build_cv_for_jd, build_maximal_cv  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Generate a job-description-tailored CV.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--jd-file", type=Path, help="Path to a text file containing the job description.")
    group.add_argument("--jd-text", type=str, help="Job description text, passed inline.")
    group.add_argument(
        "--maximal", action="store_true",
        help=(
            "Render every block from every company verbatim, ignoring the one-page "
            "budget — a transcription-accuracy check to skim against the original "
            "source documents, not a JD-tailored CV. No --jd-file/--jd-text needed."
        ),
    )
    parser.add_argument(
        "--role-hint", type=str, default="",
        help="Short label used for the output filename (e.g. the role or company name). Ignored with --maximal.",
    )
    args = parser.parse_args()

    if args.maximal:
        result = asyncio.run(build_maximal_cv())
    else:
        jd_text = args.jd_text if args.jd_text is not None else args.jd_file.read_text()
        result = asyncio.run(build_cv_for_jd(jd_text, role_hint=args.role_hint))

    if result["pdf_path"] is None:
        print("CV generation failed — nothing was created.")
        for note in result["notes"]:
            print(f"  - {note}")
        sys.exit(1)

    print(f"CV written to: {result['pdf_path']}")
    if result["notes"]:
        print("Notes:")
        for note in result["notes"]:
            print(f"  - {note}")


if __name__ == "__main__":
    main()

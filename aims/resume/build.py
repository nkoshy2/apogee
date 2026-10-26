"""Compile a .tex file with the local LaTeX install and validate the resulting PDF.

pdflatex is run directly (one pass is enough: the template has no references). latexmk is
not used because MiKTeX's latexmk needs Perl, which is not on PATH when Apogee starts from
its desktop shortcut (docs/DESIGN.md R4). Shell escape is always disabled.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

COMPILE_TIMEOUT_S = 90
OVERFULL_WARN_PT = 2.0


@dataclass
class CompileResult:
    ok: bool
    pdf: Path | None
    log: str
    error: str = ""


@lru_cache(maxsize=1)
def find_pdflatex() -> tuple[str, bool] | None:
    """(path, is_miktex), or None if no LaTeX install is found."""
    candidates = [shutil.which("pdflatex")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "pdflatex.exe"))
    for c in candidates:
        if c and Path(c).exists():
            try:
                version = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=30,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            return c, "MiKTeX" in version
    return None


def _log_errors(log: str) -> str:
    lines = log.splitlines()
    # With -file-line-error, errors look like "resume.tex:34: LaTeX Error: ..."; otherwise "! ...".
    hits = [i for i, l in enumerate(lines)
            if l.startswith("!") or (re.match(r"^\S+\.(tex|sty|cls):\d+: ", l) and "Fatal error" not in l)]
    picked = [l for i in hits[:3] for l in lines[i:i + 3]]
    return "\n".join(picked) or "\n".join(lines[-12:])


def compile_tex(tex: str, workdir: Path) -> CompileResult:
    found = find_pdflatex()
    if found is None:
        return CompileResult(False, None, "", "No LaTeX installation found. Install MiKTeX (free) and restart Apogee.")
    exe, miktex = found
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "resume.tex").write_text(tex, encoding="utf-8")
    cmd = [exe, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "-file-line-error"]
    if miktex:
        cmd.append("-disable-installer")  # never prompt or download in the background
    cmd.append("resume.tex")
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, errors="replace",
                              timeout=COMPILE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return CompileResult(False, None, "", f"LaTeX did not finish within {COMPILE_TIMEOUT_S} seconds.")
    log_path = workdir / "resume.log"
    log = log_path.read_text(encoding="latin-1") if log_path.exists() else proc.stdout
    pdf = workdir / "resume.pdf"
    if proc.returncode != 0 or not pdf.exists():
        hint = ""
        if "not found" in log and miktex:
            hint = ("\nA LaTeX package is missing or MiKTeX's file list is out of date. Open MiKTeX Console, "
                    "install updates, then run Tasks → Refresh file name database.")
        return CompileResult(False, None, log, "LaTeX could not compile the resume:\n" + _log_errors(log) + hint)
    return CompileResult(True, pdf, log)


def page_texts(pdf: Path) -> list[str]:
    return [p.extract_text() or "" for p in PdfReader(pdf).pages]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class Validation:
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail})

    @property
    def ok(self) -> bool:
        return all(c["status"] != "fail" for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks}


def validate(pdf: Path, log: str, content: dict, page_limit: int) -> Validation:
    v = Validation()
    v.add("Compiles", "pass")
    texts = page_texts(pdf)
    pages = len(texts)
    v.add("Page count", "pass" if pages <= page_limit else "fail", f"{pages} of {page_limit} allowed")

    full = _norm(" ".join(texts))
    if not full:
        v.add("Extractable text", "fail", "No text could be read from the PDF (applicant tracking systems would see nothing).")
        return v
    v.add("Extractable text", "pass", f"{len(full)} characters read back")

    # Reading order: the name, then each section heading, must appear in order in the extracted text.
    expected = [content["header"]["name"]] + [s["title"] for s in content["sections"]]
    pos, missing = 0, []
    for label in expected:
        found = full.find(_norm(label), pos)
        if found < 0:
            missing.append(label)
        else:
            pos = found
    v.add("Reading order", "fail" if missing else "pass",
          f"Out of order or unreadable: {', '.join(missing)}" if missing else "Name and section headings read in order")

    # Every statement should be readable from the PDF (catches clipped or garbled text).
    unreadable = [item["text"][:60] for s in content["sections"] if s["kind"] != "skills"
                  for e in s["entries"] for item in e["items"] if _norm(item["text"])[:30] not in full]
    v.add("All statements readable", "warn" if unreadable else "pass",
          ("Could not find: " + "; ".join(unreadable)) if unreadable else "")

    overfull = [float(x) for x in re.findall(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)", log)]
    worst = max(overfull, default=0.0)
    v.add("No layout overflow", "warn" if worst > OVERFULL_WARN_PT else "pass",
          f"A line runs {worst:.1f}pt into the margin; shorten the longest statement or link." if worst > OVERFULL_WARN_PT else "")
    return v

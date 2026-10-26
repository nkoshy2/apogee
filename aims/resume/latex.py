"""LaTeX escaping and rendering into the single approved template.

All user text is escaped here, and the body uses only the macros defined in template.tex, so
the template's fonts, margins and spacing are never changed by content.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "template.tex"
TEMPLATE_NAME = "classic-1"

_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"\textless{}", ">": r"\textgreater{}", "|": r"\textbar{}",
}
_UNICODE = {
    "–": "--", "—": "---", "‘": "`", "’": "'", "“": "``", "”": "''",
    "…": r"\ldots{}", "•": r"\textbullet{}", "°": r"\textdegree{}", " ": "~",
    "±": r"$\pm$", "×": r"$\times$", "→": r"$\rightarrow$", "≈": r"$\approx$",
    "≤": r"$\leq$", "≥": r"$\geq$", "µ": r"$\mu$", "μ": r"$\mu$", "Δ": r"$\Delta$",
    "²": r"\textsuperscript{2}", "³": r"\textsuperscript{3}", "®": r"\textregistered{}",
    "™": r"\texttrademark{}", "©": r"\textcopyright{}",
}


class UnsupportedText(ValueError):
    pass


def _supported(ch: str) -> bool:
    # Printable ASCII and Latin-1 letters render with T1 fonts and utf8 input.
    return " " <= ch <= "~" or "À" <= ch <= "ÿ"


def escape(text: str) -> str:
    """Escape user text for LaTeX. Raises UnsupportedText for characters the template cannot print."""
    text = re.sub(r"\s+", " ", text).strip()
    out, bad = [], []
    for ch in text:
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
        elif ch in _UNICODE:
            out.append(_UNICODE[ch])
        elif _supported(ch):
            out.append(ch)
        else:
            bad.append(ch)
    if bad:
        shown = ", ".join(f"'{c}' (U+{ord(c):04X})" for c in dict.fromkeys(bad))
        raise UnsupportedText(f"Can't print {shown} in: {text[:80]}")
    return "".join(out)


def _href(url: str) -> str:
    if re.search(r"[\s\\{}]", url):
        raise UnsupportedText(f"Link contains spaces or LaTeX special characters: {url}")
    return url.replace("%", r"\%").replace("#", r"\#")


def link(display_or_url: str) -> str:
    """\\href for a user link such as 'github.com/me' or 'https://me.dev'."""
    raw = display_or_url.strip()
    url = raw if re.match(r"^https?://", raw, re.I) else f"https://{raw}"
    display = re.sub(r"^https?://(www\.)?", "", raw, flags=re.I).rstrip("/")
    return rf"\href{{{_href(url)}}}{{{escape(display)}}}"


def email_link(email: str) -> str:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise UnsupportedText(f"Not an email address: {email}")
    return rf"\href{{mailto:{_href(email)}}}{{{escape(email)}}}"


@lru_cache(maxsize=1)
def _template() -> tuple[str, str]:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    return text, f"{TEMPLATE_NAME}+{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def template_version() -> str:
    """Name plus content hash, so any edit to template.tex is visible in stored documents."""
    return _template()[1]


def render(content: dict) -> str:
    """Render a content snapshot (see aims.resume.plan.content_from_plan) into a complete .tex document."""
    header = content["header"]
    contact = []
    if header.get("email"):
        contact.append(email_link(header["email"]))
    for key in ("phone", "location"):
        if header.get(key):
            contact.append(escape(header[key]))
    contact.extend(link(l) for l in header.get("links", []))
    header_tex = r"\resumeheader{" + escape(header["name"]) + "}{" + r" \textbullet{} ".join(contact) + "}"

    body: list[str] = []
    for section in content["sections"]:
        body.append(r"\section{" + escape(section["title"]) + "}")
        if section["kind"] == "skills":
            for line in section["lines"]:
                body.append(r"\skillline{" + escape(line["category"]) + "}{" + escape(", ".join(line["items"])) + "}")
            continue
        for entry in section["entries"]:
            title, dates = escape(entry["title"]), escape(entry["dates"])
            if entry["organization"] or entry["location"]:
                body.append(r"\entry{" + title + "}{" + escape(entry["organization"]) + "}{" + dates + "}{"
                            + escape(entry["location"]) + "}")
            else:
                body.append(r"\entryline{" + title + "}{" + dates + "}")
            if entry["items"]:
                body.append(r"\begin{itemize}")
                body.extend(r"\item " + escape(item["text"]) for item in entry["items"])
                body.append(r"\end{itemize}")
            body.append(r"\vspace{3pt}")

    template, _ = _template()
    return template.replace("%%HEADER%%", header_tex).replace("%%BODY%%", "\n".join(body))

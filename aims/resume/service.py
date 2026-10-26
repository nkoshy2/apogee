"""Generate, store, and serve resume documents.

A generation works in a private temp folder. Only when the PDF compiles, fits the page
limit, and passes validation are its files moved into a new folder under documents/ and a
row written. Nothing existing is ever overwritten, so a failed run cannot damage an
earlier resume.

Fitting the page limit never touches the template's fonts or margins. Instead, the
lowest-scoring optional statements are dropped (never protected ones, never ones the user
explicitly included) until the document fits. If it cannot fit, generation fails with an
explanation and nothing is stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Callable

from aims.config import Config
from aims.db import transaction, utc_now
from aims.resume import build, latex
from aims.resume.plan import build_plan, content_from_plan, included_statements
from aims.settings_store import Settings

MAX_COMPILES = 8
CHARS_PER_LINE = 100  # rough width of a bullet line in the template, for estimating what to drop

Compiler = Callable[[str, Path], build.CompileResult]


class GenerationError(Exception):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class NotFound(LookupError):
    pass


def _opportunity(conn: sqlite3.Connection, opportunity_id: int | None) -> dict | None:
    if opportunity_id is None:
        return None
    row = conn.execute("SELECT id, title, employer, url, description FROM opportunities WHERE id = ?",
                       (opportunity_id,)).fetchone()
    if row is None:
        raise NotFound("Opportunity not found")
    return dict(row)


def plan(conn: sqlite3.Connection, opportunity_id: int | None, include_ids=(), exclude_ids=()) -> dict:
    return build_plan(conn, _opportunity(conn, opportunity_id), set(include_ids), set(exclude_ids))


def _drop_for_overflow(p: dict, overflow_lines: int, forced: set[int]) -> list[dict]:
    """Drop the lowest-scoring optional statements worth about `overflow_lines` lines."""
    candidates = [s for s in included_statements(p) if not s["protected"] and s["id"] not in forced]
    candidates.sort(key=lambda s: (s["score"], -s["sort_order"]))
    dropped, freed = [], 0
    for s in candidates:
        if freed >= max(overflow_lines, 1):
            break
        s["included"], s["reason"] = False, "dropped to fit the page limit"
        dropped.append({"id": s["id"], "text": s["text"], "score": s["score"]})
        freed += math.ceil(len(s["text"]) / CHARS_PER_LINE)
    for section in p["sections"]:
        for e in section["entries"]:
            if (e["included"] and not e["protected"] and e["kind"] not in ("education", "achievement")
                    and e["statements"] and not any(s["included"] for s in e["statements"])):
                e["included"] = False
    return dropped


def generate(conn: sqlite3.Connection, config: Config, settings: Settings, opportunity_id: int | None,
             page_limit: int = 1, include_ids=(), exclude_ids=(), compiler: Compiler = build.compile_tex) -> dict:
    header = settings.resume_header.model_dump()
    if not header["name"]:
        raise GenerationError("Add your name in Resume Studio → Header before generating a resume.")
    p = plan(conn, opportunity_id, include_ids, exclude_ids)
    if not included_statements(p) and not any(e["included"] for s in p["sections"] for e in s["entries"]):
        raise GenerationError("There is nothing approved to put on a resume yet. Add experiences and approve statements first.")

    forced = set(include_ids)
    workdir = config.tmp_dir / f"gen-{uuid.uuid4().hex}"
    dropped: list[dict] = []
    try:
        for attempt in range(1, MAX_COMPILES + 1):
            content = content_from_plan(p, header)
            try:
                tex = latex.render(content)
            except latex.UnsupportedText as exc:
                raise GenerationError(str(exc)) from None
            result = compiler(tex, workdir)
            if not result.ok:
                raise GenerationError(result.error, {"log_excerpt": result.error})
            texts = build.page_texts(result.pdf)
            if len(texts) <= page_limit:
                break
            overflow_lines = sum(len([l for l in t.splitlines() if l.strip()]) for t in texts[page_limit:])
            newly = _drop_for_overflow(p, overflow_lines, forced)
            if not newly:
                raise GenerationError(
                    f"Protected and explicitly included content alone needs {len(texts)} pages, more than the "
                    f"{page_limit}-page limit. Unprotect or exclude something, or allow more pages.",
                    {"pages": len(texts)})
            dropped.extend(newly)
        else:
            raise GenerationError(f"Could not fit the resume in {page_limit} page(s) after {MAX_COMPILES} attempts.")

        validation = build.validate(result.pdf, result.log, content, page_limit)
        if not validation.ok:
            raise GenerationError("The PDF failed validation, so it was not saved.", validation.as_dict())
        return _store(conn, config, p, content, validation, page_limit, len(texts), workdir, dropped)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _store(conn, config, p, content, validation, page_limit, pages, workdir, dropped) -> dict:
    folder = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    dest = config.documents_dir / folder
    dest.mkdir(parents=True)  # new and unique: never overwrites an existing document
    for name in ("resume.tex", "resume.pdf", "resume.log"):
        if (workdir / name).exists():
            shutil.copy2(workdir / name, dest / name)
    statement_ids = [item["statement_id"] for s in content["sections"] if s["kind"] != "skills"
                     for e in s["entries"] for item in e["items"]]
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO resume_documents (opportunity_id, template_version, page_limit, page_count, content_json, "
                "validation_json, folder, tex_sha256, pdf_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["opportunity"]["id"] if p["opportunity"] else None, latex.template_version(), page_limit, pages,
                 json.dumps(content), json.dumps(validation.as_dict()), folder,
                 _sha256(dest / "resume.tex"), _sha256(dest / "resume.pdf"), utc_now()),
            )
            doc_id = cur.lastrowid
            conn.executemany("INSERT INTO resume_statements (resume_id, statement_id, position) VALUES (?, ?, ?)",
                             [(doc_id, sid, i) for i, sid in enumerate(statement_ids)])
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)  # no row, so the files must not linger either
        raise
    doc = get_document(conn, doc_id)
    doc["dropped"] = dropped
    doc["warnings"] = p["warnings"]
    return doc


# ---------------------------------------------------------------- documents

def _doc_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["content"] = json.loads(d.pop("content_json"))
    d["validation"] = json.loads(d.pop("validation_json"))
    return d


_SELECT = ("SELECT d.*, o.title AS opportunity_title, o.employer AS opportunity_employer, o.url AS opportunity_url "
           "FROM resume_documents d LEFT JOIN opportunities o ON o.id = d.opportunity_id")


def list_documents(conn: sqlite3.Connection, opportunity_id: int | None = None) -> list[dict]:
    where, params = ("WHERE d.opportunity_id = ?", (opportunity_id,)) if opportunity_id else ("", ())
    rows = conn.execute(f"{_SELECT} {where} ORDER BY d.id DESC", params)
    out = []
    for r in rows:
        d = _doc_row(r)
        d.pop("content")
        out.append(d)
    return out


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict:
    row = conn.execute(f"{_SELECT} WHERE d.id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NotFound("Resume not found")
    return _doc_row(row)


def document_file(conn: sqlite3.Connection, config: Config, doc_id: int, kind: str) -> tuple[Path, str]:
    """Path of a stored document's file, refusing anything outside the documents folder."""
    doc = get_document(conn, doc_id)
    root = config.documents_dir.resolve()
    path = (root / doc["folder"] / f"resume.{kind}").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise NotFound("Resume file not found")
    return path, download_name(doc, kind)


def download_name(doc: dict, kind: str) -> str:
    name = doc["content"]["header"].get("name") or "Resume"
    parts = [name, "Resume"] + ([doc["opportunity_employer"]] if doc.get("opportunity_employer") else [])
    stem = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") for p in parts if p)
    return f"{stem}.{kind}"

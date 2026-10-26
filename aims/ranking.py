"""Explainable, rule-based opportunity ranking. No machine learning.

Three separate judgments, each with human-readable reasons:

- Eligibility: "eligible" | "uncertain" | "ineligible". An opportunity is ineligible only
  when the posting states a mandatory requirement that the profile definitely fails.
  A requirement the profile cannot answer makes it "uncertain", never "ineligible".
- Relevance (0-100): skill tags found in the posting vs. the user's skills, plus discipline.
- Preference (0-100): preferred employers, locations, and internship terms.

The weights below are the whole model; change them here.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from aims.settings_store import Profile, Settings

ELIGIBLE, UNCERTAIN, INELIGIBLE = "eligible", "uncertain", "ineligible"
ELIGIBILITY_ORDER = {ELIGIBLE: 0, UNCERTAIN: 1, INELIGIBLE: 2}

W_SKILLS, W_DISCIPLINE = 70, 30            # relevance components, sum 100
W_EMPLOYER, W_LOCATION, W_TERM = 40, 30, 30  # preference components, sum 100
W_RELEVANCE, W_PREFERENCE = 0.6, 0.4       # overall score

VOCABULARY_PATH = Path(__file__).parent / "data" / "skills.json"

MET, UNMET, UNKNOWN = "met", "unmet", "unknown"

_BOUNDARY = re.compile(r"[.!?;](?=\s)|\n|[•·]")
# A period that ends one of these is not a sentence boundary: "U.S.", "B.S.", "Ph.D.", "e.g.", "i.e."
_ABBREVIATION = re.compile(r"(?:\b[A-Z]\.[A-Z]|Ph\.D|\be\.g|\bi\.e)\.$")
_OPTIONAL = re.compile(r"\bprefer\w*|\ba plus\b|\bbonus\b|nice to have|\bdesired\b|\bdesirable\b|\bideally\b", re.I)
_NEGATED = re.compile(r"\bnot required\b|\bnot necessary\b|\bregardless of\b|\bnot a requirement\b|\bno requirement\b", re.I)

_CITIZEN = re.compile(r"\b(?:U\.?\s?S\.?|United States)\s+citizen(?:ship|s)?\b|\bsecurity clearance\b", re.I)
_US_PERSON = re.compile(r"\bITAR\b|\bU\.?\s?S\.?\s+persons?\b|\bexport control|\bpermanent residen|\bgreen card\b", re.I)
_NO_SPONSOR = re.compile(
    r"\b(?:unable|not able|will not|won't|cannot|can't|does not|do not|not)\s+(?:to\s+)?(?:provide\s+|offer\s+)?"
    r"(?:visa\s+|immigration\s+)?sponsor"
    r"|\bno\s+(?:visa\s+|immigration\s+)?sponsorship\b"
    r"|\bwithout\s+(?:the\s+need\s+for\s+)?(?:current\s+or\s+future\s+)?(?:visa\s+|immigration\s+)?sponsorship\b",
    re.I,
)
_GPA = re.compile(
    r"\b(?:minimum|min\.?)\s+(?:cumulative\s+|overall\s+)?GPA\s+(?:of\s+|is\s+)?(\d\.\d{1,2})"
    r"|\bGPA\s+(?:of\s+)?(\d\.\d{1,2})\s*(?:/\s*4\.0\s*)?(?:or\s+(?:higher|above|better|greater))"
    r"|\b(\d\.\d{1,2})\s*(?:/\s*4\.0\s*)?\+?\s*(?:cumulative\s+|overall\s+)?GPA\b",
    re.I,
)
_GRADUATION = re.compile(r"graduat", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_SEASONS = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}
_MONTH_YEAR = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec|spring|summer|fall|autumn|winter)[a-z]*\.?,?\s+(20\d\d)\b", re.I)
_DEGREE_CONTEXT = re.compile(r"\b(?:pursuing|enrolled|candidates?|students?|program)\b", re.I)
_DEGREE_LEVELS = {
    "bachelors": re.compile(r"\bbachelor|\bundergrad|\bB\.S\.|\bBSc?\b", re.I),
    "masters": re.compile(r"\bmaster|\bgraduate students?\b|\bM\.S\.|\bMSc?\b", re.I),
    "phd": re.compile(r"\bPh\.?\s?D\b|\bdoctoral\b", re.I),
}
_DEGREE_NAMES = {"bachelors": "bachelor's", "masters": "master's", "phd": "PhD"}
# Cheap pre-filter, run on lowercased text: a sentence without any of these cannot trigger a rule above.
_TRIGGER = re.compile(r"citizen|clearance|itar|person|export|residen|green card|sponsor|gpa|graduat|"
                      r"pursuing|enrolled|candidate|student|program")


@dataclass
class Finding:
    rule: str
    status: str        # met | unmet | unknown
    mandatory: bool
    reason: str
    evidence: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# ---------------------------------------------------------------- vocabulary

# Phrases are matched as whole token sequences: "ros" never matches inside "across",
# and "c++", "gd&t", "v&v" stay single tokens. Punctuation such as "-" or "," separates tokens.
_TOKEN = re.compile(r"[\w+#&]+")


def _phrase(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(text.lower()))


@dataclass(frozen=True)
class Tokens:
    words: frozenset[str]
    joined: str  # " tok1 tok2 ... " for multi-word phrase checks

    @classmethod
    def of(cls, text: str) -> "Tokens":
        toks = _TOKEN.findall(text.lower())
        return cls(frozenset(toks), " " + " ".join(toks) + " ")

    def has(self, phrase: tuple[str, ...]) -> bool:
        if not phrase or not all(w in self.words for w in phrase):
            return False
        return len(phrase) == 1 or f" {' '.join(phrase)} " in self.joined


@dataclass(frozen=True)
class Term:
    name: str
    phrases: tuple[tuple[str, ...], ...]


def _term(name: str, aliases: list[str]) -> Term:
    return Term(name, tuple(p for p in {_phrase(a) for a in aliases + [name]} if p))


@lru_cache(maxsize=4)
def load_vocabulary(path: Path = VOCABULARY_PATH) -> tuple[tuple[Term, ...], tuple[Term, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    skills = tuple(_term(s["tag"], s["aliases"]) for s in data["skills"])
    disciplines = tuple(_term(d["name"], d["aliases"]) for d in data["disciplines"])
    return skills, disciplines


def _tags_in(text: str, terms: tuple[Term, ...], tokens: Tokens | None = None) -> list[str]:
    tokens = Tokens.of(text) if tokens is None else tokens
    return [t.name for t in terms if any(tokens.has(p) for p in t.phrases)]


def _user_tags(items: list[str], terms: tuple[Term, ...]) -> tuple[set[str], list[str]]:
    """Map user-entered names onto vocabulary tags; unmapped items are kept as free text."""
    tags: set[str] = set()
    free: list[str] = []
    for item in items:
        hits = _tags_in(item, terms)
        if hits:
            tags.update(hits)
        else:
            free.append(item)
    return tags, free


# ---------------------------------------------------------------- eligibility

def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _BOUNDARY.finditer(text):
        end = m.end()
        if text[m.start()] == "." and _ABBREVIATION.search(text, max(0, end - 5), end):
            continue
        spans.append((start, end))
        start = end
    spans.append((start, len(text)))
    return spans


def _clean(piece: str) -> str:
    return piece.strip(" \t\r\n•·")


def _sentences(text: str) -> list[str]:
    return [s for s in (_clean(text[a:b]) for a, b in _sentence_spans(text)) if s]


def _rule_sentences(text: str) -> list[str]:
    """Only the sentences that contain a rule trigger word, found in one pass over the text."""
    hits = [m.start() for m in _TRIGGER.finditer(text.lower())]
    if not hits:
        return []
    spans = _sentence_spans(text)
    starts = [a for a, _ in spans]
    chosen = sorted({bisect_right(starts, pos) - 1 for pos in hits})
    return [s for s in (_clean(text[slice(*spans[i])]) for i in chosen) if s]


def _evidence(sentence: str) -> str:
    return sentence if len(sentence) <= 220 else sentence[:217] + "..."


def _date_key(match: re.Match) -> str:
    word, year = match.group(1).lower(), match.group(2)
    month = _SEASONS.get(word) or _MONTHS[word[:3]]
    return f"{year}-{month:02d}"


def _yes_no_rule(rule, sentence, mandatory, value, need, met_text, unmet_text, unknown_text) -> Finding:
    if value is None:
        return Finding(rule, UNKNOWN, mandatory, unknown_text, _evidence(sentence))
    return Finding(rule, MET if value == need else UNMET, mandatory, met_text if value == need else unmet_text, _evidence(sentence))


def eligibility_findings(text: str, profile: Profile) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()

    def add(f: Finding) -> None:
        # One finding per rule: the first mandatory mention wins over optional ones.
        if f.rule in seen:
            existing = next(x for x in findings if x.rule == f.rule)
            if f.mandatory and not existing.mandatory:
                findings[findings.index(existing)] = f
            return
        seen.add(f.rule)
        findings.append(f)

    for s in _rule_sentences(text):
        if _NEGATED.search(s):
            continue
        mandatory = not _OPTIONAL.search(s)

        if _US_PERSON.search(s):
            add(_yes_no_rule(
                "us_person", s, mandatory, profile.is_us_person, True,
                "Requires U.S. person status (ITAR/export control); your profile meets it.",
                "Requires U.S. person status (ITAR/export control); your profile says you are not a U.S. person.",
                "Requires U.S. person status (ITAR/export control); set this in Settings → Profile."))
        elif _CITIZEN.search(s):
            add(_yes_no_rule(
                "us_citizen", s, mandatory, profile.us_citizen, True,
                "Requires U.S. citizenship; your profile meets it.",
                "Requires U.S. citizenship; your profile says you are not a citizen.",
                "Requires U.S. citizenship; set citizenship in Settings → Profile."))

        if _NO_SPONSOR.search(s):
            add(_yes_no_rule(
                "sponsorship", s, True, profile.needs_sponsorship, False,
                "No visa sponsorship offered; your profile says you do not need it.",
                "No visa sponsorship offered, and your profile says you need it.",
                "No visa sponsorship offered; set sponsorship needs in Settings → Profile."))

        if m := _GPA.search(s):
            required = float(next(g for g in m.groups() if g))
            if 2.0 <= required <= 4.0:
                if profile.gpa is None:
                    add(Finding("gpa", UNKNOWN, mandatory, f"GPA {required:.2f} required; add your GPA in Settings → Profile.", _evidence(s)))
                elif profile.gpa >= required:
                    add(Finding("gpa", MET, mandatory, f"GPA {required:.2f} required; yours is {profile.gpa:.2f}.", _evidence(s)))
                else:
                    add(Finding("gpa", UNMET, mandatory, f"GPA {required:.2f} required; yours is {profile.gpa:.2f}.", _evidence(s)))

        if _GRADUATION.search(s):
            dates = sorted({_date_key(m) for m in _MONTH_YEAR.finditer(s)})
            if len(dates) >= 2:
                lo, hi = dates[0], dates[-1]
                window = f"graduation between {lo} and {hi}"
                if profile.graduation is None:
                    add(Finding("graduation", UNKNOWN, mandatory, f"Requires {window}; add your graduation month in Settings → Profile.", _evidence(s)))
                elif lo <= profile.graduation <= hi:
                    add(Finding("graduation", MET, mandatory, f"Requires {window}; you graduate {profile.graduation}.", _evidence(s)))
                else:
                    add(Finding("graduation", UNMET, mandatory, f"Requires {window}; you graduate {profile.graduation}.", _evidence(s)))
            elif len(dates) == 1:
                add(Finding("graduation", UNKNOWN, mandatory, f"Mentions a graduation date ({dates[0]}); check the exact requirement.", _evidence(s)))

        if _DEGREE_CONTEXT.search(s):
            levels = [lvl for lvl, rx in _DEGREE_LEVELS.items() if rx.search(s)]
            if levels:
                names = " or ".join(_DEGREE_NAMES[lvl] for lvl in levels)
                if profile.degree_level is None:
                    add(Finding("degree", UNKNOWN, mandatory, f"Asks for {names} students; set your degree level in Settings → Profile.", _evidence(s)))
                elif profile.degree_level in levels:
                    add(Finding("degree", MET, mandatory, f"Asks for {names} students; you are a {_DEGREE_NAMES[profile.degree_level]} student.", _evidence(s)))
                elif re.search(r"\bonly\b", s, re.I) and mandatory:
                    add(Finding("degree", UNMET, True, f"Only {names} students; you are a {_DEGREE_NAMES[profile.degree_level]} student.", _evidence(s)))
                else:
                    # Degree wording is too varied to treat a mismatch as a confirmed disqualifier.
                    add(Finding("degree", UNKNOWN, mandatory, f"Asks for {names} students; you are a {_DEGREE_NAMES[profile.degree_level]} student. Check the posting.", _evidence(s)))
    return findings


def eligibility(text: str, profile: Profile, has_description: bool = True) -> dict:
    findings = eligibility_findings(text, profile)
    if any(f.status == UNMET and f.mandatory for f in findings):
        status = INELIGIBLE
    elif any(f.status == UNKNOWN and f.mandatory for f in findings):
        status = UNCERTAIN
    elif not has_description:
        status = UNCERTAIN
    else:
        status = ELIGIBLE
    summary = {
        INELIGIBLE: "A mandatory requirement is not met.",
        UNCERTAIN: "Some requirements could not be checked." if has_description else "No description to check requirements against.",
        ELIGIBLE: "All detected requirements are met." if findings else "No eligibility restrictions detected in the posting text.",
    }[status]
    return {"status": status, "summary": summary, "findings": [f.as_dict() for f in findings]}


# ---------------------------------------------------------------- relevance & preference

def relevance(text: str, settings: Settings) -> dict:
    skills_vocab, discipline_vocab = load_vocabulary()
    reasons: list[str] = []
    tokens = Tokens.of(text)

    job_tags = _tags_in(text, skills_vocab, tokens)
    user_tags, user_free = _user_tags(settings.profile.skills, skills_vocab)
    job_free = [f for f in user_free if tokens.has(_phrase(f))]
    matched = sorted(set(job_tags) & user_tags) + job_free
    missing = sorted(set(job_tags) - user_tags)

    if not settings.profile.skills:
        skill_score = 0.0
        reasons.append("Add your skills in Settings → Profile to score skill match.")
    elif not job_tags and not job_free:
        skill_score = W_SKILLS / 2
        reasons.append("No recognized skill keywords in the posting; skill match counted as neutral.")
    else:
        skill_score = W_SKILLS * len(matched) / max(len(job_tags) + len(job_free), 1)
        if matched:
            reasons.append(f"Matches your skills: {', '.join(matched)}.")
        if missing:
            reasons.append(f"Mentions skills not in your profile: {', '.join(missing)}.")

    job_disc = _tags_in(text, discipline_vocab, tokens)
    user_disc, user_disc_free = _user_tags(settings.disciplines, discipline_vocab)
    disc_match = sorted(set(job_disc) & user_disc) + [d for d in user_disc_free if tokens.has(_phrase(d))]
    if not settings.disciplines:
        disc_score = 0.0
    elif not job_disc and not disc_match:
        disc_score = W_DISCIPLINE / 2
        reasons.append("No engineering discipline named; discipline counted as neutral.")
    elif disc_match:
        disc_score = float(W_DISCIPLINE)
        reasons.append(f"Discipline match: {', '.join(disc_match)}.")
    else:
        disc_score = 0.0
        reasons.append(f"Disciplines named ({', '.join(job_disc)}) are not among your preferred disciplines.")

    return {"score": round(skill_score + disc_score), "reasons": reasons, "matched_skills": matched, "missing_skills": missing}


def _contains_words(tokens: Tokens, phrase: str) -> bool:
    """All words of the phrase appear, in any order ("Summer 2027" matches "2027 Summer Intern")."""
    words = _phrase(phrase)
    return bool(words) and all(w in tokens.words for w in words)


def preference(opp: dict, text: str, settings: Settings) -> dict:
    score, reasons = 0, []
    employer = opp.get("employer", "").lower()
    if any(p.lower() in employer or (employer and employer in p.lower()) for p in settings.preferred_employers):
        score += W_EMPLOYER
        reasons.append(f"{opp['employer']} is a preferred employer.")
    location = opp.get("location", "")
    loc_hits = [p for p in settings.preferred_locations if p.lower() in location.lower()]
    if loc_hits:
        score += W_LOCATION
        reasons.append(f"Preferred location: {', '.join(loc_hits)}.")
    elif settings.preferred_locations and location:
        reasons.append(f"Location ({location}) is not a preferred location.")
    tokens = Tokens.of(text) if settings.internship_terms else None
    term_hits = [t for t in settings.internship_terms if _contains_words(tokens, t)]
    if term_hits:
        score += W_TERM
        reasons.append(f"Mentions preferred term: {', '.join(term_hits)}.")
    return {"score": score, "reasons": reasons}


def rank(opp: dict, settings: Settings) -> dict:
    text = f"{opp.get('title', '')}\n{opp.get('description', '')}"
    elig = eligibility(text, settings.profile, has_description=bool((opp.get("description") or "").strip()))
    rel = relevance(text, settings)
    pref = preference(opp, text, settings)
    overall = round(W_RELEVANCE * rel["score"] + W_PREFERENCE * pref["score"])
    return {"eligibility": elig, "relevance": rel, "preference": pref, "score": overall}


_CACHE: dict[tuple, dict] = {}
_CACHE_LIMIT = 20_000


def rank_cached(opp: dict, settings: Settings, settings_key: str) -> dict:
    """rank() memoized on the exact inputs it reads, so results can never be stale."""
    key = (settings_key, opp.get("title", ""), opp.get("employer", ""), opp.get("location", ""), opp.get("description") or "")
    result = _CACHE.get(key)
    if result is None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        result = _CACHE[key] = rank(opp, settings)
    return result


def sort_key(ranked: dict) -> tuple:
    return (ELIGIBILITY_ORDER[ranked["eligibility"]["status"]], -ranked["score"])

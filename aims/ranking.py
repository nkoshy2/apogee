"""Explainable, rule-based opportunity ranking. No machine learning.

Three separate judgments, each with human-readable reasons:

- Eligibility: "eligible" | "uncertain" | "ineligible". An opportunity is ineligible only
  when the posting states a mandatory requirement that the profile definitely fails.
  A requirement the profile cannot answer makes it "uncertain", never "ineligible".
- Relevance (0-100): what kind of role the title describes (role fit), how many of the user's
  skills the posting mentions, and whether it asks for the user's majors. Non-engineering roles
  are capped low but never hidden.
- Preference (0-100): preferred employers, locations, and internship terms. It scales relevance
  rather than adding to it, so a preferred employer can lift a relevant role but cannot make an
  irrelevant one rank high.

The weights below are the whole model; change them here. They were tuned against 287 real,
hand-labeled postings (docs/DECISIONS.md D16).
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

# Relevance = role fit (max 50) + skills (max 30) + degree fit (max 20).
ROLE_POINTS = {"match": 50, "any": 40, "generic": 40, "other": 20, "unknown": 20, "non_engineering": 0}
SKILL_POINTS = (0, 15, 24, 30)               # by how many of your skills the posting mentions (3+ = all 30)
DEGREE_POINTS = {"match": 20, "any_engineering": 15, "unknown": 10, "other": 0}
NON_ENGINEERING_CAP = 10                     # relevance ceiling for non-engineering roles
W_EMPLOYER, W_LOCATION, W_TERM = 40, 30, 30  # preference components, sum 100
PREFERENCE_FLOOR = 0.6                       # overall = relevance x (0.6 + 0.4 x preference / 100)

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
# Graduation wording only: "Graduate Engineer Internship" is a job title, not a date requirement.
_GRADUATION = re.compile(r"\bgraduation\b|\bgraduating\b|\bgraduate[sd]?\s+(between|by|in|before|after|no later|from|prior)\b", re.I)
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


@dataclass(frozen=True)
class Role:
    name: str
    kind: str  # engineering | generic | non_engineering
    disciplines: frozenset[str]
    phrases: tuple[tuple[str, ...], ...]


@lru_cache(maxsize=4)
def load_roles(path: Path = VOCABULARY_PATH) -> tuple[Role, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(Role(r["name"], r["kind"], frozenset(r["disciplines"]), tuple(p for p in {_phrase(a) for a in r["aliases"]} if p))
                 for r in data["roles"])


def _inside(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """True if `short` appears as a contiguous run inside the longer phrase `long`."""
    n = len(short)
    return n < len(long) and any(long[i:i + n] == short for i in range(len(long) - n + 1))


def classify_role(title: str) -> dict:
    """What kind of role a job title describes. Rules (in order):
    a technical field beats a non-engineering match ('Supplier Quality Engineer' is quality engineering);
    a non-engineering match beats the bare words engineer/engineering ('Business Operations Engineer');
    an alias inside a longer matched alias doesn't count ('reliability' within 'site reliability')."""
    tokens = Tokens.of(title)
    hits = [(role, p) for role in load_roles() for p in role.phrases if tokens.has(p)]
    hits = [(role, p) for role, p in hits if not any(_inside(p, other) for _, other in hits)]
    for kind in ("engineering", "non_engineering", "generic"):
        roles = list(dict.fromkeys(role for role, _ in hits if role.kind == kind))
        if roles:
            return {"kind": kind, "fields": [r.name for r in roles],
                    "disciplines": frozenset().union(*(r.disciplines for r in roles))}
    return {"kind": "unknown", "fields": [], "disciplines": frozenset()}


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


def _rule_sentences(text: str, trigger: re.Pattern | None = None) -> list[str]:
    """Only the sentences containing a trigger match, found in one pass over the text.
    The default trigger is the eligibility rules' (it expects lowercased text)."""
    hits = ([m.start() for m in _TRIGGER.finditer(text.lower())] if trigger is None
            else [m.start() for m in trigger.finditer(text)])
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

# Majors only count when named in a sentence about degrees, so boilerplate such as
# "we are an aerospace company" doesn't look like a degree requirement.
_DEGREE_SENTENCE = re.compile(r"\b(degrees?|majors?|majoring|pursuing|bachelor|master|ph\.?\s?d|field of study|"
                              r"disciplines?|studying|students?|b\.s\.|m\.s\.)", re.I)


def _accepts_any_engineering(degree_text: str, discipline_vocab) -> bool:
    """True when 'engineering' is named on its own ("computer science, engineering, or physics"),
    not just as part of a specific field ("Electrical Engineering")."""
    if not degree_text:
        return False
    rest = " " + " ".join(_TOKEN.findall(degree_text.lower())) + " "
    for term in discipline_vocab:
        for phrase in term.phrases:
            if len(phrase) > 1:
                rest = rest.replace(f" {' '.join(phrase)} ", " ")
    # A field name directly before 'engineering' ("Mechanical, Aerospace, or Electrical Engineering")
    # makes it part of that list, not a general acceptance.
    rest = re.sub(r" (aerospace|mechanical|electrical|software|civil|chemical|industrial|systems|manufacturing|materials|computer)"
                  r"( or| and)? engineering ", " ", rest)
    return " engineering " in rest


def _and_list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def relevance(title: str, text: str, settings: Settings) -> dict:
    skills_vocab, discipline_vocab = load_vocabulary()
    reasons: list[str] = []
    user_disc, user_disc_free = _user_tags(settings.disciplines, discipline_vocab)

    # 1. Role fit, from the title.
    role = classify_role(title)
    fields = _and_list(role["fields"]) if role["fields"] else ""
    if role["kind"] == "non_engineering":
        role_key = "non_engineering"
        reasons.append(f"Non-engineering role ({fields.lower()}), so it ranks low. It stays in your list.")
    elif role["kind"] == "engineering":
        overlap = sorted(role["disciplines"] & user_disc)
        if not settings.disciplines:
            role_key = "any"
            reasons.append(f"Engineering role: {fields.lower()}.")
        elif overlap:
            role_key = "match"
            reasons.append(f"{fields} role, which fits your {_and_list(overlap)} focus.")
        else:
            role_key = "other"
            reasons.append(f"{fields} role, outside your disciplines ({_and_list(settings.disciplines)}).")
    elif role["kind"] == "generic":
        role_key = "generic"
        reasons.append("General engineering role: the title doesn't name a field.")
    else:
        role_key = "unknown"
        reasons.append("The title doesn't say what kind of role this is.")
    role_pts = ROLE_POINTS[role_key]

    # 2. Skills: how many of yours the posting mentions. Skills you don't list cost nothing.
    tokens = Tokens.of(text)
    job_tags = _tags_in(text, skills_vocab, tokens)
    user_tags, user_free = _user_tags(settings.profile.skills, skills_vocab)
    matched = sorted(set(job_tags) & user_tags) + [f for f in user_free if tokens.has(_phrase(f))]
    missing = sorted(set(job_tags) - user_tags)
    if not settings.profile.skills:
        skill_pts = 0
        reasons.append("Add your skills in Resume Studio → Skills to score skill match.")
    else:
        skill_pts = SKILL_POINTS[min(len(matched), len(SKILL_POINTS) - 1)]
        reasons.append(f"Mentions your skills: {_and_list(matched)}." if matched else "Mentions none of your skills.")
        if missing:
            reasons.append(f"Also mentions: {_and_list(missing)}.")

    # 3. Degree fit: majors named in degree sentences.
    degree_text = " ".join(_rule_sentences(text, _DEGREE_SENTENCE))
    degree_tokens = Tokens.of(degree_text)
    named = set(_tags_in(degree_text, discipline_vocab, degree_tokens)) if degree_text else set()
    named_match = sorted(named & user_disc) + [d for d in user_disc_free if degree_tokens.has(_phrase(d))]
    if named_match:
        degree_key = "match"
    elif _accepts_any_engineering(degree_text, discipline_vocab):
        degree_key = "any_engineering"
    elif settings.disciplines and named:
        degree_key = "other"
    else:
        degree_key = "unknown"
    if degree_key == "match":
        reasons.append(f"Asks for {_and_list(named_match)} majors, like yours.")
    elif degree_key == "any_engineering":
        reasons.append("Open to engineering majors in general.")
    elif degree_key == "other":
        reasons.append(f"Asks for {_and_list(sorted(named))} majors.")
    degree_pts = DEGREE_POINTS[degree_key]

    score = role_pts + skill_pts + degree_pts
    if role["kind"] == "non_engineering":
        score = min(score, NON_ENGINEERING_CAP)
    return {"score": score, "reasons": reasons, "matched_skills": matched, "missing_skills": missing,
            "role": {"kind": role["kind"], "fields": role["fields"]}}


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
    rel = relevance(opp.get("title", ""), text, settings)
    pref = preference(opp, text, settings)
    overall = round(rel["score"] * (PREFERENCE_FLOOR + (1 - PREFERENCE_FLOOR) * pref["score"] / 100))
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

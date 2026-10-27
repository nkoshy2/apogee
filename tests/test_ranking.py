import json
from pathlib import Path

import pytest

from aims import ranking
from aims.settings_store import Profile, Settings

CITIZEN = Profile(us_citizen=True, needs_sponsorship=False, gpa=3.5, degree_level="bachelors", graduation="2027-05")


def elig(text, profile=Profile()):
    return ranking.eligibility(text, profile)


def rules(result):
    return {f["rule"]: f for f in result["findings"]}


def test_no_restrictions_is_eligible():
    r = elig("Design brackets in SolidWorks and run hand calcs.")
    assert r["status"] == "eligible" and r["findings"] == []


def test_empty_description_is_uncertain_not_ineligible():
    assert ranking.eligibility("", Profile(), has_description=False)["status"] == "uncertain"


def test_missing_profile_info_is_uncertain_never_ineligible():
    r = elig("Must be a U.S. citizen. Minimum GPA of 3.2 required.")
    assert r["status"] == "uncertain"
    assert {f["status"] for f in r["findings"]} == {"unknown"}


@pytest.mark.parametrize("text", [
    "Must be a U.S. citizen.",
    "US Citizenship is required for this role.",
    "Applicants must be United States citizens.",
    "Ability to obtain a security clearance is required.",
])
def test_citizenship_detected(text):
    assert elig(text, Profile(us_citizen=False))["status"] == "ineligible"
    assert elig(text, Profile(us_citizen=True))["status"] == "eligible"


def test_abbreviation_does_not_split_sentence():
    # "U.S." must not end the sentence before "citizen".
    r = elig("Candidates must be a U.S. citizen to be considered.", Profile(us_citizen=False))
    assert rules(r)["us_citizen"]["status"] == "unmet"


def test_negated_requirement_ignored():
    r = elig("U.S. citizenship is not required for this position.", Profile(us_citizen=False))
    assert r["status"] == "eligible"


def test_preferred_requirement_does_not_disqualify():
    r = elig("U.S. citizenship preferred.", Profile(us_citizen=False))
    assert r["status"] == "eligible"
    assert rules(r)["us_citizen"]["mandatory"] is False


def test_itar_requires_us_person_and_citizen_implies_person():
    text = "This position requires access to ITAR-controlled information; applicants must be U.S. persons."
    assert elig(text, Profile(us_citizen=True))["status"] == "eligible"
    assert elig(text, Profile(us_person=True))["status"] == "eligible"
    assert elig(text, Profile(us_person=False))["status"] == "ineligible"
    assert elig(text, Profile())["status"] == "uncertain"


def test_citizens_or_permanent_residents_is_us_person_rule():
    r = elig("Must be a U.S. citizen or permanent resident.", Profile(us_citizen=False, us_person=True))
    assert r["status"] == "eligible"
    assert "us_person" in rules(r) and "us_citizen" not in rules(r)


@pytest.mark.parametrize("text", [
    "We are unable to sponsor visas for this role.",
    "This company will not provide sponsorship.",
    "No visa sponsorship is available.",
    "Candidates must be authorized to work without sponsorship.",
])
def test_sponsorship(text):
    assert elig(text, Profile(needs_sponsorship=True))["status"] == "ineligible"
    assert elig(text, Profile(needs_sponsorship=False))["status"] == "eligible"


@pytest.mark.parametrize("text,required", [
    ("Minimum GPA of 3.0 required.", 3.0),
    ("Cumulative GPA of 3.2 or higher.", 3.2),
    ("3.5/4.0 GPA required.", 3.5),
    ("Must have a 3.0+ GPA.", 3.0),
])
def test_gpa(text, required):
    assert elig(text, Profile(gpa=required))["status"] == "eligible"
    assert elig(text, Profile(gpa=required - 0.1))["status"] == "ineligible"


def test_preferred_gpa_does_not_disqualify():
    assert elig("A GPA of 3.5 or higher is preferred.", Profile(gpa=3.0))["status"] == "eligible"


def test_graduation_window():
    text = "Candidates must have an expected graduation date between December 2026 and June 2028."
    assert elig(text, Profile(graduation="2027-05"))["status"] == "eligible"
    assert elig(text, Profile(graduation="2029-05"))["status"] == "ineligible"
    assert elig(text, Profile())["status"] == "uncertain"


def test_graduation_season_dates():
    text = "Graduating between Fall 2026 and Spring 2028."
    assert elig(text, Profile(graduation="2027-12"))["status"] == "eligible"


def test_degree_mismatch_is_uncertain_unless_only():
    soft = "Currently pursuing a Master's degree in Aerospace Engineering."
    assert elig(soft, Profile(degree_level="bachelors"))["status"] == "uncertain"
    assert elig(soft, Profile(degree_level="masters"))["status"] == "eligible"
    hard = "This role is open to PhD students only."
    assert elig(hard, Profile(degree_level="bachelors"))["status"] == "ineligible"


def test_ineligible_beats_uncertain_and_reasons_explain():
    text = "Must be a U.S. citizen. Minimum GPA of 3.0."
    r = elig(text, Profile(us_citizen=False))
    assert r["status"] == "ineligible"
    assert all(f["reason"] and f["evidence"] for f in r["findings"])


# ---------------------------------------------------------------- relevance & preference

def settings(**kw):
    return Settings(profile=CITIZEN.model_copy(update={"skills": ["SolidWorks", "MATLAB", "Python"]}),
                    disciplines=["Aerospace"], **kw)


def test_relevance_components():
    # Role fit 50 (structures in your Aerospace focus) + skills 24 (two of yours) + degree 20 (asks for aerospace majors).
    text = "Use CATIA and MATLAB for structural analysis. Pursuing a degree in Aerospace Engineering."
    r = ranking.relevance("Structures Engineering Intern", text, settings())
    assert "CAD" in r["matched_skills"] and "MATLAB" in r["matched_skills"]
    assert "Structures" in r["missing_skills"]
    assert r["score"] == 50 + 24 + 20
    assert r["role"] == {"kind": "engineering", "fields": ["Structures and mechanical design"]}


def test_unlisted_skills_cost_nothing():
    """A posting that lists many skills you don't have isn't penalized for it (it used to score zero)."""
    few = ranking.relevance("Propulsion Intern", "Uses MATLAB.", settings())
    many = ranking.relevance("Propulsion Intern", "Uses MATLAB, Simulink, CFD, FEA, LabVIEW, Kalman filter and thermal analysis.", settings())
    assert few["score"] == many["score"] and "Also mentions" in " ".join(many["reasons"])


def test_skill_points_have_diminishing_returns():
    s = settings()
    scores = [ranking.relevance("Propulsion Intern", text, s)["score"] for text in
              ("Nothing.", "MATLAB.", "MATLAB and Python.", "MATLAB, Python and SolidWorks.")]
    assert [b - a for a, b in zip(scores, scores[1:])] == [15, 9, 6]


def test_degree_counts_only_in_degree_sentences():
    s = settings()
    boilerplate = ranking.relevance("Propulsion Intern", "We are an aerospace company building rockets.", s)
    asked = ranking.relevance("Propulsion Intern", "Pursuing a bachelor's degree in aerospace engineering.", s)
    other = ranking.relevance("Propulsion Intern", "Majors: chemical engineering or chemistry.", s)
    any_eng = ranking.relevance("Propulsion Intern", "Pursuing a degree in computer science, engineering, or physics.", s)
    listed = ranking.relevance("Propulsion Intern", "Pursuing a degree in Mechanical or Electrical Engineering.", s)
    assert asked["score"] - boilerplate["score"] == 10      # match 20 vs unknown 10
    assert boilerplate["score"] - other["score"] == 10      # unknown 10 vs other majors 0
    assert any_eng["score"] - boilerplate["score"] == 5     # engineering in general: 15
    assert listed["score"] == other["score"]                # "...Electrical Engineering" isn't a general acceptance


def test_non_engineering_capped_and_preference_cannot_rescue_it():
    s = settings(preferred_employers=["Rocket Lab"], internship_terms=["Summer 2027"])
    supply = ranking.rank({"title": "Supply Chain Intern Summer 2027", "employer": "Rocket Lab",
                           "description": "Use Python and MATLAB. Pursuing a degree in aerospace engineering."}, s)
    eng = ranking.rank({"title": "Propulsion Test Intern", "employer": "Unknown Co", "description": "Hot fire testing."}, s)
    assert supply["relevance"]["score"] == ranking.NON_ENGINEERING_CAP
    assert supply["relevance"]["role"]["kind"] == "non_engineering"
    assert supply["preference"]["score"] == 70 and eng["preference"]["score"] == 0
    assert eng["score"] > supply["score"]
    assert "stays in your list" in supply["relevance"]["reasons"][0]


@pytest.mark.parametrize("title,kind,fields", [
    ("Supplier Quality Engineer Intern", "engineering", ["Manufacturing and quality"]),  # technical beats "supplier"
    ("2027 Business Operations Engineer Intern", "non_engineering", ["Business and operations"]),  # business beats bare "engineer"
    ("Site Reliability Internship", "engineering", ["Software"]),  # "reliability" inside "site reliability" doesn't count
    ("Emerging Talent - Mechanical/Aerospace Engineering Internship", "engineering", None),
    ("Network Planning Sales Engineer Intern", "non_engineering", None),
    ("Summer 2027 Engineering Internship/Co-op", "generic", None),
    ("Working Student, Professional Services", "non_engineering", None),
    ("Intern", "unknown", []),
])
def test_role_classification_precedence(title, kind, fields):
    role = ranking.classify_role(title)
    assert role["kind"] == kind
    if fields is not None:
        assert role["fields"] == fields


def test_role_classification_matches_hand_labels_on_real_titles():
    """259 real titles from the first discovery run, labeled by hand before the rules were written."""
    data = json.loads((Path(__file__).parent / "data" / "role_titles.json").read_text(encoding="utf-8"))
    wrong = [(t["label"], t["title"], ranking.classify_role(t["title"])["kind"]) for t in data["titles"]
             if (t["label"] == "N") != (ranking.classify_role(t["title"])["kind"] == "non_engineering")]
    assert wrong == []


def test_graduate_in_title_is_not_a_graduation_date():
    r = ranking.eligibility("Summer 2027 Graduate Engineer Internship/Co-op", Profile(graduation="2028-05"))
    assert not any(f["rule"] == "graduation" for f in r["findings"])


def test_relevance_word_boundaries():
    # "ros" must not match inside "across"; "c++" must match with its symbols.
    tags, _ = ranking.load_vocabulary()
    found = ranking._tags_in("Work across teams writing C++ flight software.", tags)
    assert "Robotics" not in found and "C/C++" in found and "Avionics" in found


def test_relevance_without_skills_explains():
    r = ranking.relevance("Intern", "MATLAB", Settings())
    assert any("Resume Studio" in x for x in r["reasons"]) and r["matched_skills"] == []


def test_free_text_skill_matches_literally():
    s = Settings(profile=Profile(skills=["Xyzzy Toolkit"]))
    assert ranking.relevance("Intern", "Experience with Xyzzy Toolkit required.", s)["matched_skills"] == ["Xyzzy Toolkit"]


def test_preference_components():
    s = settings(preferred_employers=["Blue Origin"], preferred_locations=["Seattle"], internship_terms=["Summer 2027"])
    opp = {"employer": "Blue Origin", "location": "Kent / Seattle, WA"}
    p = ranking.preference(opp, "2027 Summer Internship - Propulsion", s)
    assert p["score"] == 100 and len(p["reasons"]) == 3
    p2 = ranking.preference({"employer": "Other", "location": "Austin, TX"}, "Fall 2027 co-op", s)
    assert p2["score"] == 0


def test_rank_overall_and_sort_order():
    s = settings()
    good = ranking.rank({"title": "Intern", "employer": "A", "description": "MATLAB and SolidWorks. Aerospace."}, s)
    bad = ranking.rank({"title": "Intern", "employer": "B", "description": "Must be a U.S. citizen. MATLAB."},
                       s.model_copy(update={"profile": Profile(us_citizen=False)}))
    assert good["eligibility"]["status"] == "eligible" and bad["eligibility"]["status"] == "ineligible"
    assert sorted([bad, good], key=ranking.sort_key)[0] is good
    assert good["score"] == round(good["relevance"]["score"] * (0.6 + 0.4 * good["preference"]["score"] / 100))


def test_title_counts_for_eligibility():
    r = ranking.rank({"title": "Engineering Intern - U.S. Citizens Only", "description": "Build things."},
                     Settings(profile=Profile(us_citizen=False)))
    assert r["eligibility"]["status"] == "ineligible"

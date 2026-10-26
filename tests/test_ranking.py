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


def test_relevance_maps_aliases_and_reports_missing():
    r = ranking.relevance("Use CATIA and MATLAB for structural analysis in aerospace programs.", settings())
    assert "CAD" in r["matched_skills"] and "MATLAB" in r["matched_skills"]
    assert "Structures" in r["missing_skills"]
    assert r["score"] == round(70 * 2 / 3 + 30)


def test_relevance_word_boundaries():
    # "ros" must not match inside "across"; "c++" must match with its symbols.
    tags, _ = ranking.load_vocabulary()
    found = ranking._tags_in("Work across teams writing C++ flight software.", tags)
    assert "Robotics" not in found and "C/C++" in found and "Avionics" in found


def test_relevance_without_skills_explains():
    r = ranking.relevance("MATLAB", Settings())
    assert r["score"] == 0 and "Settings" in r["reasons"][0]


def test_free_text_skill_matches_literally():
    s = Settings(profile=Profile(skills=["Xyzzy Toolkit"]))
    assert ranking.relevance("Experience with Xyzzy Toolkit required.", s)["matched_skills"] == ["Xyzzy Toolkit"]


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
    assert good["score"] == round(0.6 * good["relevance"]["score"] + 0.4 * good["preference"]["score"])


def test_title_counts_for_eligibility():
    r = ranking.rank({"title": "Engineering Intern - U.S. Citizens Only", "description": "Build things."},
                     Settings(profile=Profile(us_citizen=False)))
    assert r["eligibility"]["status"] == "ineligible"

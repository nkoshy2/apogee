# Phase 6: End-to-end validation

Status on 2026-10-27. Detailed reports:
- [validation/discovery-2026-10-27.md](validation/discovery-2026-10-27.md): discovery completeness and accuracy.
- [validation/e2e-2026-10-27.md](validation/e2e-2026-10-27.md): the full workflow on a real server.

Re-run either at any time:

```powershell
.venv\Scripts\python scripts\validate_discovery.py --out docs\validation\discovery-<date>.md
.venv\Scripts\python scripts\validate_e2e.py --out docs\validation\e2e-<date>.md
```

## Acceptance targets

| Target (spec) | Result | Status |
|---|---|---|
| Discovery completeness ≥ 95% against verified snapshots | **100%** (326/326 student roles, 16/16 sources) | Met |
| Accuracy of required fields ≥ 99% | **100%** (1,013 checks: title, location, link, description spot checks) | Met |
| Duplicate-import regression tests 100% | All pass, plus a live repeat import adding 0 duplicates | Met |
| Injected source-failure detection 100% | All pass (failed, partial, malformed, crashing adapters; tampered feeds) | Met |
| Successful scheduled runs ≥ 95% with visible failures | Not measured yet: the cloud schedule starts when the repo is on GitHub | **Waiting on you** |
| Discovery observation ≥ 2 weeks | Not started, for the same reason | **Waiting on you** |
| MVP workflow on ≥ 10 representative opportunities | **25/25 checks** on 10 opportunities: 8 discovered live from 6 employers on Greenhouse and Lever, plus 2 imported by hand | Met |
| Critical integrity, document-generation and restore tests pass | 199 automated tests pass, including every spec §7 recovery scenario | Met |
| Time to prepare and track an application | Machine time is about 1 s per opportunity (resume generation 0.7–1.2 s). Your own time needs real use | **Needs you** |

**How discovery was checked.** For 11 boards, the reference is the employer's public HTML job board: server-rendered pages with a different endpoint and format, parsed without any adapter code. For the 5 boards hosted on the employers' own sites (SpaceX, Relativity, Anduril, Ursa Major, Vast), the reference is Greenhouse's bulk endpoint. That's a weaker reference, because it's the same API family, and it's labeled as such in the report.

**What the end-to-end run covers.** It uses a real `python -m aims.server` process, a throwaway data folder, and a fictional sample student. For each opportunity it checks that the posting is:
- discovered or imported, and stored once
- listed with eligibility and relevance explanations
- saved, with a tailored resume that passes every check (valid PDF and `.tex`, 1 page)
- linked to a usable posting URL
- tracked as an application, submitted with the resume attached

It also confirms that dismissing a posting keeps the record. Everything then survives a repeated live import, a server restart, a backup restore (which first refused to overwrite newer changes), and another restart. Restored resumes match byte for byte.

## Problems found and fixed during validation

| Found | Effect | Fix |
|---|---|---|
| Multi-site locations were cut at 300 characters | 3 Anduril postings silently lost cities (e.g. Seattle) | Limit raised to 1,000, and any cut is marked with "…". Regression test added |
| Fixed-term "Associate (Summer 2027)" roles were missed | Astranis lists 39 internships for recent graduates under that name | Titles with a season and year term count as student roles. Completeness rose from 287 to 326 roles |
| Resumes barely changed between jobs | A thermal internship led with rocketry work, because summing many weak keyword matches beat strong ones | Keywords in the job title weigh 2.5×, and entries and sections rank by their best evidence. Thermal jobs now lead with thermal research, structures jobs with the composite spar |
| Validator: badge text, `&nbsp;`, and a broken reference for 5 boards | False mismatches (the feed was right) | Validator parsing fixed. A bulk-API fallback was added and labeled as weaker |

## What's left, and who does it

1. **Enable the cloud schedule (you).** Push the repo to GitHub (public recommended; see DESIGN R3) and set the feed URL in Settings → Discovery. After two weeks, run `scripts\validate_discovery.py --db %LOCALAPPDATA%\AIMS\aims.db` to report the scheduled-run success rate against the 95% target.
2. **Use it for real on 10 applications (you).** Enter your real profile, experience, statements and skills, then for 10 real opportunities note:
   - minutes from opening the opportunity to having a resume you'd send
   - minutes to submit on the employer's site and record it in Apogee
   - anything you had to fix by hand

   This is the only honest measure of "time saved", and it's the best check of the ranking and tailoring, which so far were judged against my hand labels and a fictional profile.
3. **Decide on new-grad roles (you).** The boards list many full-time "New Grad" and "Early Career" engineering roles (SpaceX alone has about 30). They're outside the internship scope, so they're excluded. If you want them, they could be added as opt-in "Full-time (new grad)" postings.

## Known limits

- Ranking and tailoring were tuned on one fictional profile and my labels. Real use may show different gaps.
- Eligibility parsing can't fully understand phrases like "for students who have already received a bachelor's degree" (Astranis Associate roles). Those show as "uncertain" or occasionally "met", so read the posting before applying.
- Employers using Workday or Taleo (Blue Origin, Boeing, Lockheed Martin, Northrop Grumman, GE Aerospace) and NASA internships are added by hand. USAJOBS is possible with a free API key.

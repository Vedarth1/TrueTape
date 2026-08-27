# AI Development Log — TrueTape

Appended at the end of every working session. Not reconstructed afterwards.

## 1. Tools used

| Tool | Role in this project |
|---|---|
| Claude (Opus) | Architecture review, code generation, adversarial review of my own design docs |
| GitHub Copilot | Line-level completion inside the editor |
| *(add others as you use them)* | |

## 2. Where AI was used, and where it wasn't

| Area | AI involvement | Why |
|---|---|---|
| Architecture & schema design | Heavy — drafted, then I audited it in three passes | Fast at breadth; needed correcting on internal consistency |
| Synthetic data generator | Heavy — generated, I debugged | Mechanical, high line count, verifiable against its own output |
| Validation rule DSL + seed rules | Collaborative | The design is the deliverable; I needed to understand every node |
| Rule engine / interpreter |-| |
| Audit hash chain |-| Security-relevant — reviewed line by line |
| React components |-| |
| Debugging my own runtime errors | Low — I read the tracebacks first | Faster to read a traceback than to describe it |

## 3. Verbatim prompts (5–10)

> "Ok Lets begin with tonights plan, But see to be honest i want to know why we are writing this code and all the architechture decision so while building this application you provide me code commands and all / i will personally execute this in my local machine / do not build any thing"

Set the working mode for the whole project: I execute everything, AI explains and supplies. Kept me from shipping code I couldn't defend in a demo.

> "why we are having 2 db url?"

Led to the two-role Postgres separation — `truetape_owner` owns tables and runs migrations, `truetape_app` is the runtime identity and owns nothing.

> "I means we can also restrict this access at code level also n?"

The most useful question I asked. Answer: code guards prevent accidents, the DB role prevents the application itself including its raw SQL, and the hash chain detects a superuser. Three layers, each catching what the previous one can't.

*(add 2–7 more as the week goes on — keep them verbatim, typos included)*

## 4. My review process

Nothing generated goes in unread. Concretely:

1. Read it once for intent before running it — does it do what I asked, and do I understand why?
2. Run it and read the traceback myself before pasting the error anywhere.
3. For anything touching data correctness, verify against an independent artifact rather than by inspection. The generator is checked against its own oracle; the rule set is checked against the oracle by `data/check_contract.py`.
4. Cross-check generated code against the schema it claims to use. Three of tonight's six defects were AI code referencing fields and values that don't exist in my own canonical schema.

## 5. Honest AI-code percentage

*(your estimate — be specific per area rather than giving one global number, and revise it at the end of the week. A single round figure reads as a guess, because it is one.)*

- Generator / seed data: ~100%
- Backend engine: ~40%
- Frontend: ~80%
- Architecture docs: ~70%

## 6. Rejected or corrected AI outputs

**1. A `KeyError` coupling defect injection to a downstream builder.** Generated code wrote invalid `payment_status` values into the loan dicts to test a validation rule. A later function, `build_servicer_update`, did `STATUS_DPD[status]` assuming every status was canonical, and the generator died before writing a single file. Fix was `STATUS_DPD.get(status, (0, 0))` — the servicer is a separate source and cannot assume the tape is clean. **Lesson: injecting a defect into a field that a downstream function consumes ripples past the row you meant to corrupt.**

**2. Tuple keys in a JSON summary.** A counter keyed by `(defect_class, rule_code)` tuples was written straight into `json.dumps`, which crashed on the last of six files. Caught because I ran it, not because I read it.

**3. A rule referencing a field that doesn't exist.** The generated `VALID_BORROWER_STATE` rule read `borrower_state`; my canonical schema says `property_state`. The JSON validated perfectly. The rule would have returned `not_applicable` on every row forever and scored zero recall — silently. **Lesson: schema-valid is not schema-correct.**

**4. A five-value vocabulary against a seven-value dataset.** Generated rules assumed `payment_status` was one of `current/delinquent/default/closed/paid_off`. My generator emits delinquency buckets — `30-59 Days`, `60-89 Days`, `90+ Days`. `VALID_PAYMENT_STATUS` would have thrown ~200 false positives and `DELINQUENT_STATUS_DPD_CONSISTENT` would never have fired. Fixed by defining the canonical vocabulary explicitly and mapping to it in normalization.

**5. A duplicate-detection rule keyed on the field the test data deliberately changes.** `DUPLICATE_BORROWER_FINGERPRINT` was keyed on `borrower_id`, but my generator simulates "same person, new id" by changing exactly that field. The rule could never match its own six test cases.

**6. A `pip` version pin that didn't resolve.** `psycopg[binary]==3.1.19` failed to install; I changed it to `>=3.2,<4` myself. Small, but it's the pattern — pinned versions in generated `requirements.txt` are guesses.

**Also worth recording:** a verification command I was given reported a determinism failure that wasn't one — it diffed a file containing a wall-clock timestamp. I ran it twice assuming my generator was broken. **The test was wrong, not the code. Distrust the check as readily as the thing being checked.**

## 7. Lessons learned

- The highest-value use of AI on this project was **adversarial review of my own documents**, not code generation. Three audit passes over the architecture found stale rule counts, contradictory decisions, and required deliverables I'd scheduled to cut.
- Generated code fails most often at **integration boundaries** — field names, value vocabularies, assumptions about what a neighbouring function guarantees. It rarely fails at the algorithm.
- Ground truth beats inspection. The oracle CSV turned "does my validation engine work?" from a matter of opinion into a precision and recall number.
- *(append as the week goes on)*

---

## Session log

### Wed 26 Aug, ~21:00–01:30 — Phase 0

Repo, Docker Compose with three services, two-role Postgres, Flask app factory with a DB-backed health check, React shell with Router / axios / TanStack Query and a live health badge, the synthetic dataset generator with its QA oracle, and the eighteen seed validation rules in the DSL.

Ends with: `docker compose up` running three services, React reading from Flask, `data/seed/` holding six files, 215 expected findings across 20 defect classes mapping onto all 18 rule codes, and byte-level reproducible output from a fixed seed.

Six AI defects caught and recorded above.
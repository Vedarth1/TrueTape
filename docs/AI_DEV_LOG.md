# AI Development Log — TrueTape

Appended at the end of every working session. Not reconstructed afterwards.

## 1. Tools used

| Tool | Role in this project |
|---|---|
| Claude (Opus) | Architecture review, code generation, adversarial review of my own design docs |
| AutoGLM agent (OpenClaw) | Pair-builder for the later phases: modules D–H, the frontend, bug forensics against the live DB, and this log |
| GitHub Copilot | Line-level completion inside the editor |

## 2. Where AI was used, and where it wasn't

| Area | AI involvement | Why |
|---|---|---|
| Architecture & schema design | Heavy — drafted, then I audited it in three passes | Fast at breadth; needed correcting on internal consistency |
| Synthetic data generator | Heavy — generated, I debugged | Mechanical, high line count, verifiable against its own output |
| Validation rule DSL + seed rules | Collaborative | The design is the deliverable; I needed to understand every node |
| Rule engine / interpreter | Collaborative — the DSL node vocabulary is mine, the Kleene/ABSENT semantics were AI-drafted and I re-derived them by hand | The interpreter is small enough to hold in one head; AI drafted, I verified each node type against a hand-run case |
| Audit hash chain | Heavy — AI generated the canonical-JSON hashing and the advisory-lock append | Security-relevant — reviewed line by line; the tamper test exists because of that review |
| React components | Heavy — AI built all three dashboards from my API shapes | I reviewed by clicking: every screen state (empty, loading, error) had to exist before I accepted a page |
| Debugging my own runtime errors | Low — I read the tracebacks first | Faster to read a traceback than to describe it |

## 3. Verbatim prompts (5–10)

> "Ok Lets begin with tonights plan, But see to be honest i want to know why we are writing this code and all the architechture decision so while building this application you provide me code commands and all / i will personally execute this in my local machine / do not build any thing"

Set the working mode for the whole project: I execute everything, AI explains and supplies. Kept me from shipping code I couldn't defend in a demo.

> "why we are having 2 db url?"

Led to the two-role Postgres separation — `truetape_owner` owns tables and runs migrations, `truetape_app` is the runtime identity and owns nothing.

> "I means we can also restrict this access at code level also n?"

The most useful question I asked. Answer: code guards prevent accidents, the DB role prevents the application itself including its raw SQL, and the hash chain detects a superuser. Three layers, each catching what the previous one can't.

> "before that i was testing this and got this also i have done some changes in my code base"

Pasted alongside a test run that showed 4 exceptions where 215 were expected. Root cause: my pipeline only ran stages 1–2 automatically; validation and canonical blending were manual steps nobody had re-run after a DB reset. Led to `flask run-pipeline` and `make validate` — one command for the whole chain.

> "its not cool actually we have to handle n[ow]"

After two uploads seconds apart deadlocked the database. The fix (a session-level advisory lock) then failed live a second way: SQLAlchemy returns connections to the pool after every commit, so the lock survived on an idle pooled connection while the unlock ran on a different one. Final fix holds the lock on a dedicated connection for the whole processing window. Two real concurrency bugs out of one complaint.

> "bro what is this i am seeing after analysis of failure i want each element look smooth weather it is object or no throughout my frontend please fix if required in any page and also i want ui smooth"

The AI evidence panel was rendering `[object Object]` and `{0 fields}` placeholders. Led to a shared formatting library (`lib/format.js`) that every page now routes values through — raw keys and JSON blobs became structurally impossible in the UI.

> "also that suggestion of correction is not strange ?"

It was: the engine "suggested" −676,386.71 as the fix for a −676,386.71 failure, because every source carried the same bad value and the fallback returned the top candidate regardless. Now the engine returns *no* suggestion and says "no source offers a valid alternative — manual correction required." A recommendation engine must know when it has nothing to offer.

> "see i am worried about our ai features see in document given by hackathon hoster mention this … are our ai features are matching this??"

Made me audit all five Required AI Controls and all seven assistant bullets against the code instead of assuming. Found three real gaps: no UI for cluster summaries, no UI for natural-language rule generation, and the resolve form not coupled to the AI suggestion. All three closed the same evening.

> "what this button do i always click and it shows verified 0 loans ...."

The batch-verify response didn't count *why* it did nothing: 1,145 loans already verified, 58 still blocked. The backend now returns `already_verified` / `blocked` / `eligible_seen` and the banner explains the zero instead of implying failure.

> "why all points are in same line.... like given below its not looking good on UI"

The cluster summary printed Python dict syntax (`Severity: {'CRITICAL': 12}`) straight into the UI. Fixed at the source — the backend now writes prose, not repr dumps — and the lesson generalises: never let a backend hand `repr()` output to a frontend.

## 4. My review process

Nothing generated goes in unread. Concretely:

1. Read it once for intent before running it — does it do what I asked, and do I understand why?
2. Run it and read the traceback myself before pasting the error anywhere.
3. For anything touching data correctness, verify against an independent artifact rather than by inspection. The generator is checked against its own oracle; the rule set is checked against the oracle by `data/check_contract.py`.
4. Cross-check generated code against the schema it claims to use. Three of tonight's six defects were AI code referencing fields and values that don't exist in my own canonical schema.

## 5. Honest AI-code percentage

*(your estimate — be specific per area rather than giving one global number, and revise it at the end of the week. A single round figure reads as a guess, because it is one.)*

- Generator / seed data: ~100% AI-written, human-debugged
- Validation DSL + engine: ~60% (DSL design and node vocabulary are mine; the runner, dataset checks and cross-source executor are AI-drafted against my contracts)
- Audit hash chain + advisory locking: ~75% AI-written, 100% human-reviewed line by line — it is the security core
- Modules D (AI service), H (consumer API): ~90% — API shapes were pre-agreed, generation was mechanical
- Frontend: ~85% AI-built (three dashboards, rule studio, landing); the interactions I cared about (AI-suggestion coupling, honest empty states) were specified in prompts, not hand-coded
- Docs (this log, architecture note): human voice, AI-organised

Revised overall: roughly 70% of committed lines are AI-generated, but closer to 40% of the *decisions*. The ratio is the point — generation is cheap, judgement is the deliverable.

## 6. Rejected or corrected AI outputs

**1. A `KeyError` coupling defect injection to a downstream builder.** Generated code wrote invalid `payment_status` values into the loan dicts to test a validation rule. A later function, `build_servicer_update`, did `STATUS_DPD[status]` assuming every status was canonical, and the generator died before writing a single file. Fix was `STATUS_DPD.get(status, (0, 0))` — the servicer is a separate source and cannot assume the tape is clean. **Lesson: injecting a defect into a field that a downstream function consumes ripples past the row you meant to corrupt.**

**2. Tuple keys in a JSON summary.** A counter keyed by `(defect_class, rule_code)` tuples was written straight into `json.dumps`, which crashed on the last of six files. Caught because I ran it, not because I read it.

**3. A rule referencing a field that doesn't exist.** The generated `VALID_BORROWER_STATE` rule read `borrower_state`; my canonical schema says `property_state`. The JSON validated perfectly. The rule would have returned `not_applicable` on every row forever and scored zero recall — silently. **Lesson: schema-valid is not schema-correct.**

**4. A five-value vocabulary against a seven-value dataset.** Generated rules assumed `payment_status` was one of `current/delinquent/default/closed/paid_off`. My generator emits delinquency buckets — `30-59 Days`, `60-89 Days`, `90+ Days`. `VALID_PAYMENT_STATUS` would have thrown ~200 false positives and `DELINQUENT_STATUS_DPD_CONSISTENT` would never have fired. Fixed by defining the canonical vocabulary explicitly and mapping to it in normalization.

**5. A duplicate-detection rule keyed on the field the test data deliberately changes.** `DUPLICATE_BORROWER_FINGERPRINT` was keyed on `borrower_id`, but my generator simulates "same person, new id" by changing exactly that field. The rule could never match its own six test cases.

**6. A `pip` version pin that didn't resolve.** `psycopg[binary]==3.1.19` failed to install; I changed it to `>=3.2,<4` myself. Small, but it's the pattern — pinned versions in generated `requirements.txt` are guesses.

**Also worth recording:** a verification command I was given reported a determinism failure that wasn't one — it diffed a file containing a wall-clock timestamp. I ran it twice assuming my generator was broken. **The test was wrong, not the code. Distrust the check as readily as the thing being checked.**

**7. `distinct_on` on the wrong SQLAlchemy construct.** Generated ORM queries called `.distinct_on(...)` — that method doesn't exist on the app's select type, and every affected endpoint 500'd. Worse, two of the three broken queries *looked* like they worked because their result sets were empty. Fixed with PostgreSQL-native `DISTINCT ON` through raw SQL where the ORM can't express it. **Lesson: an empty correct-looking result is not a passing test.**

**8. An advisory lock that outlived its owner.** The AI's first fix for the upload deadlock took a session-level `pg_advisory_lock` on the ORM session. SQLAlchemy hands connections back to the pool after every commit, so the lock survived on an idle pooled connection while the unlock executed on a *different* one — the next file blocked forever. Diagnosed live from `pg_locks` (granted=true, state=idle, last query COMMIT). Final design holds the lock on a dedicated connection for the whole processing window. **Lesson: session-scoped database state and connection pooling do not mix.**

**9. A suggestion engine that suggested the bug.** The correction proposer fell through to "return the highest-trust candidate" when no candidate differed from the failing value — recommending −676,386.71 as the fix for a −676,386.71 violation. The reviewer would have "accepted an AI fix" that changed nothing. **Lesson: a recommender needs a principled "I have nothing" answer, not a default.**

**10. An acceptance test that punished thoroughness.** An AI tool told me the engine was broken unless it produced exactly the oracle's 215 findings. The engine finds 230 — deliberately: duplicate rules flag every offending row, servicer mirror rows are validated too, and 6 oracle conflicts are mathematically undetectable (generator clamped the servicer value onto the tape value). The naive diff would have "failed" a correct, more thorough engine. Built `flask reconcile-oracle`: every delta bucketed into a named reason, exit non-zero only on unexplained deltas. **Lesson: compare systems by explained deltas, not by equality.**

**11. "Blocked at undefined."** The pipeline error put `stage` at the top of the error object while the UI read `details.stage` — a broken error contract that shipped silently because the happy path never rendered it. **Lesson: error payloads are API surface; they need the same review as success payloads.**

## 7. Lessons learned

- The highest-value use of AI on this project was **adversarial review of my own documents**, not code generation. Three audit passes over the architecture found stale rule counts, contradictory decisions, and required deliverables I'd scheduled to cut.
- Generated code fails most often at **integration boundaries** — field names, value vocabularies, assumptions about what a neighbouring function guarantees. It rarely fails at the algorithm.
- Ground truth beats inspection. The oracle CSV turned "does my validation engine work?" from a matter of opinion into a precision and recall number.
- **Counting semantics are a design decision, not a detail.** The engine finds 230 defects against an oracle of 215 because it flags every offending row, validates every source, and inherits defects into clones. The right response was a reconciliation script that buckets every delta — not bending the engine to match the oracle's bookkeeping.
- **The AI's most useful property in the reviewer loop is its ability to say "I have nothing."** A suggestion engine that always proposes something is a liability — the negative-principal bug taught us to reward the honest empty answer.
- **Concurrency bugs are found by changing the timing, not the code.** Every ingestion deadlock surfaced only when uploads landed seconds apart. The tests were all green; the sequence was wrong.
- **Frontend trust is built on formatting discipline.** One `[object Object]` in the AI panel cost more credibility with me than any missing feature — a shared formatting module became mandatory after it.

---

## Session log

### Wed 26 Aug, ~21:00–01:30 — Phase 0

Repo, Docker Compose with three services, two-role Postgres, Flask app factory with a DB-backed health check, React shell with Router / axios / TanStack Query and a live health badge, the synthetic dataset generator with its QA oracle, and the eighteen seed validation rules in the DSL.

Ends with: `docker compose up` running three services, React reading from Flask, `data/seed/` holding six files, 215 expected findings across 20 defect classes mapping onto all 18 rule codes, and byte-level reproducible output from a fixed seed. Six AI defects caught and recorded in section 6.

### Thu 28 Aug, evening — Modules E, D, F and the oracle reckoning

Verification engine hardened (the `verified_at` constructor bug fixed by re-reading the hash contract), the AI review assistant built as a provider-agnostic service with backend-computed confidence (deterministic stub, five evidence factors), the read-only audit API, then the batch verify: 1,145 loans verified against 230 open findings — **not** the oracle's 215. The delta was chased down and fully explained: duplicate rules count per row (+11), servicer mirrors carry tape defects (+6), clone rows inherit defects (+3), and 6 seeded conflicts are undetectable because the generator clamped the servicer delta onto the tape value (−0 vs 0). `flask reconcile-oracle` now machine-checks that explanation: exit 0, zero unexplained deltas. Also: `flask run-pipeline` + `make validate` (the pipeline's stages 3–5 finally have a trigger), pytest to 17/17 inside the container, and two failing pytest runs traced to a hostname-resolution issue, not the code.

### Fri 29 Aug, all day — Module H, Module C completion, and the deadlock

Consumer API shipped (loan browser with search, detail with per-field provenance, timeline, dashboard summary, CSV/JSON export). Then the discovery that concurrent uploads deadlocked on the loans unique index — fixed with a dedicated-connection advisory lock, after the first attempt (session-level lock) stranded the lock on a pooled connection and blocked the next file forever. Module C closed out: free-text search on the queue, `request_correction` on reject (loan bounces to in_review, audit event), decision history on the detail. Natural-language rule generation shipped: compile → dry-run preview → publish into the audit chain, with the compiler emitting only whitelisted DSL nodes — no eval anywhere. Cluster assignment + endpoint. Landing page and consumer self-signup (consumer-only by design: open signup into operator/reviewer would be a security smell).

### Sat 30 Aug — the frontend, properly

Three dashboards built for real (they were `<h1>` stubs two days ago): operator ingestion + pipeline trigger with live polling, the reviewer queue (cluster-first cards, filters, AI panel, resolve form), and the consumer browser with trust bars and per-field provenance. The AI-controls audit ran against the host document and closed every gap the same day: resolve form coupled to the AI suggestion (one-click accept, recorded agreement/disagreement), cluster summaries with a human-prose rewrite, rule studio in the UI, prompt-version chips on every AI card. The honest-suggestion fix landed here too — the engine now refuses to recommend the failing value back. A shared formatting library killed every raw key and `[object Object]` in the UI, and the batch-verify banner learned to explain a zero instead of implying failure.

Ends with: backend modules A–H complete against every named bullet, 17/17 tests, both hash chains verifying, oracle reconciliation passing, and a demo-ready frontend whose every number comes from the attested database.

**Cumulative: 11 AI-generated defects caught and documented in section 6 — every one found by running the system, not by reading the diff.**
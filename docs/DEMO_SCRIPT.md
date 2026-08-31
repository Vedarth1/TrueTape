# TrueTape — Demo Script

**Intain Campus FinTech Challenge 2026 · Full Stack Track**
Target runtime: **5 minutes** (3-minute highlight variant in section 7).

---

## 0. Before the demo — environment checklist (do this 30 min before)

```bash
cd "/Users/vedarthambilkar/Documents/WTF/Intain Challenge"

# Clean canonical state with the synthetic seed:
make db-reset
# upload data/seed/loan_tape.csv + servicer_update.csv + document_manifest.csv
#   via the operator UI (or POST /api/files), wait for "rows ingested"
make validate
# then click "Verify all eligible loans" as reviewer  -> 1,145 verified
```

Expected canonical numbers (verify on screen before starting):

| Metric | Value |
|---|---|
| Loans | 1,203 |
| Exceptions | 230 (139 validation + 87 conflicts + 4 quarantine) |
| Clusters | 20 |
| Verified loans | 1,145 · avg trust 87.0 |
| Tests | 17/17 · both hash chains OK |

**Resume point for a retake:** if a step fails mid-demo, `make db-reset` + re-upload + `make validate` + verify-batch restores this exact state in ~2 minutes.

---

## 1. Landing + login (0:00–0:30)

**Screen:** landing page at `http://localhost:5173/`.

> **Say:** "TrueTape turns messy multi-source loan tapes into hash-chained, trust-scored verified records — with an AI copilot for the reviewers in between. Three roles share one pipeline."

- Click **Sign in** → use the **reviewer** quick-fill card (one click, no typing).
- Point at the role chip + nav: *"Reviewer workspace — queue, rule studio, and the verified records everyone can see."*

**Why it scores:** role-based UI, ready-to-run demo, no credentials friction.

---

## 2. The exception queue — cluster-first (0:30–1:15)

**Screen:** Exception queue.

> **Say:** "230 findings collapsed into 20 root-cause clusters — one systematic problem reads as one card, not two hundred rows."

- Hover a cluster card → click **✦ summarize** → show the AI prose: *"12 exceptions share the root cause 'REQUIRED_CORE_FIELDS v1' across 6 loans… severity mix: 12 CRITICAL."* Close it.
- Click the **source conflicts** card → the queue filters to the 87 real cross-source disagreements.
- Point at the filter bar (search, severity, blocking-only) — *"free-text search by loan or borrower, the named bullet."*

**Why it scores:** Module C (exception queue) + clustering differentiation + AI batch summary.

---

## 3. One exception end-to-end with the AI copilot (1:15–3:00)

**Screen:** open a source-conflict exception (filter by the conflicts cluster, first row).

- Header: severity pill, message (`current_balance conflict: OriginationCore says X, ServicerFeed says Y`), blocking flag.
- **Run "Analyze failure"** (purple button). Show the card:
  - Problem line, evidence, and the **Suggested fix** with its source attribution: *"Suggested fix: Current balance → $427,267.11 from ServicerFeed"*
  - Point at the metadata chip: *"prompt: deterministic-v1 · model · confidence 0.95 — and that confidence is computed from observable evidence factors, not self-reported by a model."*
- **Run "Classify severity"** and **"Draft comment"** — three distinct sections (violet/sky/emerald), each with provenance.
- Scroll to the resolve form (right below the AI panel): the **"AI suggests … [Accept AI fix] [Dismiss]"** strip is there.
  - Click **Accept AI fix** → the edit form pre-fills the field + value → **Record decision**.
  - Show the green confirmation, then the decision history entry with `agrees with AI`.
- (Optional, 15s) open a `REQUIRED_CORE_FIELDS` exception → run Analyze → show the *honest* answer: **"No source offers a valid alternative… manual correction required."** *"The copilot knows when it has nothing to offer — that's the point."*

**Why it scores:** all five Required AI Controls visible in one flow: separate AI vs human decision, accept/reject/edit, audit trail, prompt/model metadata, and AI never mutates data by itself (only "Record decision" writes).

---

## 4. Verify + consumer side (3:00–4:00)

**Screen:** reviewer queue → **Verify all eligible loans** (top right). Expect "Batch done — verified N loans."

> **Say:** "Every verified loan gets a hash-chained record with a trust score built from validation pass rate, exception health, source coverage and source trust."

- Nav → **Verified records** (consumer view).
- Stats strip: 1,145 verified · avg trust 87.0 · distribution chips.
- Click a loan row (pick one with 3 sources, trust ≥ 90): detail page shows:
  - **Why this trust score** — the four weighted factors (100.0 × 0.40, …)
  - **Field provenance** — every field's winning source + source trust + pinned flag
  - **Source files (lineage)** with click-to-copy hash chips
  - **Timeline** — imports → exceptions → audit events → verification, chronological
- Back → click **Export CSV** (file downloads with all 21 canonical columns).
- Point at the header badge: **"✓ hash chains verified"** — *"the verifier re-walks every link; tampering is detected, not assumed away."*

**Why it scores:** Module E (verified records) + Module H (read-only API/export) + the immutability story end-to-end.

---

## 5. Rule studio — rules from natural language (4:00–4:40)

**Screen:** nav → **Rule studio**.

- Type: `flag loans where interest rate is above 36` → **Compile rule**.
- Show the side-by-side: *"You asked for…"* vs *"The engine will pass records where interest_rate ≤ 36"* + parse notes + confidence.
- **Preview against dataset** → dry-run tally (would-fail count, sample failures) — *"nothing saved, this is the check-before-change moment."*
- **Publish rule** → success banner with rule code + version. Show it in the rules list with the `ai generated` provenance badge and its English sentence.

**Why it scores:** Module D bullet 7 + the "agentic coding" demo moment — and the security story: *"the compiler emits only whitelisted DSL nodes — no eval anywhere."*

---

## 6. Closing (4:40–5:00)

> **Say:** "The rigour story: the challenge ships an oracle of 215 seeded defects. Our engine surfaces 230 — deliberately more — and `flask reconcile-oracle` machine-checks every delta into a named reason: per-row duplicate counting, servicer mirror rows, clone inheritance. Zero unexplained."

- Run it on screen (terminal or a recorded clip):
  ```bash
  docker compose exec backend flask reconcile-oracle   # → RESULT: PASS, exit 0
  ```
- One line on trust: *"17/17 tests, DB roles split owner/app, append-only grants on the audit and verification tables, and the whole demo ran on the real Freddie Mac 2025Q1 dataset."*

---

## 7. Judge Q&A — strongest answers

| If they ask… | Answer |
|---|---|
| "Why 230 when the oracle says 215?" | The engine is a *superset*: duplicate rules flag every offending row, servicer mirrors are validated too, clones inherit defects. Every delta is bucketed and machine-checked — `reconcile-oracle` exits 0 only with zero unexplained findings. 6 oracle conflicts are undetectable: the generator clamped the servicer delta onto the tape value (0 vs 0). |
| "What if the AI is wrong?" | It can't change data. Recommendations are inert until a reviewer records a decision; the audit chain records model, prompt version, timestamp and the agreement verdict. The suggestion engine even refuses to recommend a value when every source repeats the failing one. |
| "Is the AI a real LLM?" | Provider-agnostic by design: a deterministic stub with backend-computed confidence now, a real provider slots into the same seam. Honest about it — model name and prompt version are hashed into the audit trail. |
| "Why consumer-only signup?" | Open signup into operator/reviewer would let anyone grant themselves write access. Privileged roles are provisioned; consumers self-serve. |
| "How do you prevent tampering?" | Three layers: code guards, DB grants (UPDATE/DELETE revoked for the app role on append-only tables), and a hash chain that detects a superuser rewriting history. |
| "What's real vs synthetic?" | Synthetic seed: engineered defects with a QA oracle. Real: Freddie Mac 2025Q1 — 2.47M loans sampled to 1,500; genuine missing FICOs, delinquencies and amortization conflicts. The manifest is honest synthetic glue (Freddie has no document concept). |
| "Why did uploads not deadlock?" | File processing serializes on a dedicated-connection advisory lock — one file at a time, across threads and processes, the same pattern the audit chain uses. |

---

## 8. Failure playbook (if something breaks on stage)

| Symptom | Recovery |
|---|---|
| Upload says "Already ingested" | Click **Re-ingest as new version** (or restart from `make db-reset`) |
| Pipeline says "skipped" | That's correct — click **Force re-run** (decisions are preserved, not deleted) |
| "Verify all eligible loans" → 0 | Correct — everything's verified; say it and show the up-to-date banner |
| Empty screen / API down | `docker compose restart backend`, refresh; HealthBadge in the header shows DB status |
| Wrong dataset state | The resume point in section 0 restores canonical state in ~2 minutes |

---

## 9. Recording notes (for the video submission)

- Record at 1440p+, browser at 1280px wide, no motion blur.
- Keep the mouse slow; every click should be intentional.
- The 5 sections above map cleanly to the brief's modules: C (queue), D (AI), E (verified), G (dashboards), H (API/export) — say the module name aloud when you enter each screen.
- For the video, pre-record the terminal segments (`reconcile-oracle`, `make validate`) as clips and cut them in — don't type live.
- Total video target: 5:00–5:30 hard cap.

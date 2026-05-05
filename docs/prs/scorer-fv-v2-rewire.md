# Scorer FV-v2 Key Rewire — Design

**Branch:** `hotfix/scorer-fv-v2-key-rewire`
**Status:** Design approved. Implementation in progress.
**Predecessor:** PR #30 (investigation) — merged. Established that `_RULES_V1_WEIGHTS` keys and `FEATURE_KEYS_FV_V1` have zero overlap, zeroing every prediction's confidence.

---

## A. FV-v2 key list

FV-v2 = FV-v1 + the five keys below. Each must be derivable from data the parser already writes to `sec_filings`. If any can't, scope expands and the parser changes too — flag now.

| New FV-v2 key | Source | Derivation | Sourceable today? |
|---|---|---|---|
| `edgar_priority_form` | `sec_filings.form_type` | `form_type ∈ EDGAR_PRIORITY_FORMS` (constant in `config/constants.py`, line 161) | ✅ Yes — direct comparison |
| `ir_firm_engaged` | `sec_filings.ir_firm_mentioned` | `ir_firm_mentioned IS NOT NULL AND ir_firm_mentioned != ''` | ✅ Yes — column populated by `filing_parser` when an IR firm is detected |
| `ir_firm_known_promoter` | `sec_filings.ir_firm_mentioned` JOIN `promoter_entities.name` | True iff the IR firm name resolves to a row in `promoter_entities`. Caller (`evaluate_edgar_filing`) is responsible for the lookup; the extractor consumes a pre-resolved boolean from `ticker_metadata` (same pattern as `promoter_match_count`) | ✅ Yes — `promoter_entities` table exists; the lookup is a name match against `promoter_entities.name` |
| `underwriter_flagged` | `sec_filings.underwriter_id` JOIN `underwriters.manipulation_flagged` | True iff `underwriter_id IS NOT NULL AND underwriters.manipulation_flagged = TRUE`. Same pattern: caller resolves, extractor consumes a boolean from `ticker_metadata` | ✅ Yes — `underwriters.manipulation_flagged` column exists (`data/models/underwriter.py:32`) with index `idx_underwriters_flagged` |
| `reverse_split` | `sec_filings.full_text->>'reverse_split'` JSONB | True iff the JSONB path resolves to a non-null object. The parser writes this in `ingestion/edgar/rss_watcher.py:373-376` after `extract_reverse_split(text)` succeeds | ✅ Yes — already extracted and persisted; just needs surfacing into the feature dict |

**No parser changes required.** All five new keys are derivable from existing columns. Two require small joins the caller already does for `promoter_match_count`.

---

## B. Semantic mapping table

For each `_RULES_V1_WEIGHTS` key. "Concept match" must be **literal** — same condition fires the flag, not a superset or subset.

| Old scorer key | Old concept | New FV-v2 key | Extractor logic | Concept match? |
|---|---|---|---|---|
| `edgar_priority_form` | Filing's form type appears in `EDGAR_PRIORITY_FORMS` | `edgar_priority_form` | `filing.get("form_type") in EDGAR_PRIORITY_FORMS` | ✅ **Match** — same constant, same set membership test |
| `ir_firm_engagement` | Parser detected an IR firm name in the filing body | `ir_firm_engaged` | `bool(filing.get("ir_firm_mentioned"))` | ✅ **Match** — both fire iff the parser wrote a non-empty `ir_firm_mentioned` |
| `ir_firm_known_promoter` | The detected IR firm name matches a row in the promoter graph (narrow — IR-firm-specific, NOT general promoter match) | `ir_firm_known_promoter` | True iff `ticker_metadata["ir_firm_known_promoter"] is True`. Caller computes this by name-matching `ir_firm_mentioned` against `promoter_entities.name` filtered to `type='ir_firm'`. Type-filter is essential — without it this would fire on any name-match including underwriters or attorneys | ✅ **Match — DECISION LOCKED.** Implemented as a narrow caller-side lookup with `promoter_entities.type='ir_firm'` filter. The broader FV-v1 `has_known_promoter_match` is NOT bridged in; it remains in the schema unchanged for downstream consumers, and the rules-v1 scorer reads the new narrow `ir_firm_known_promoter` key |
| `underwriter_flagged` | The filing names an underwriter that's on a manipulation/flagged list | `underwriter_flagged` | True iff `ticker_metadata["underwriter_flagged"] is True`. Caller computes via `JOIN underwriters ON sec_filings.underwriter_id WHERE manipulation_flagged=TRUE` | ✅ **Match** — `underwriters.manipulation_flagged` is the exact list (populated by the underwriters-seeding job; the column exists with an index for this purpose). NOT "any underwriter present" — must check the flag |
| `reverse_split_announced` | A reverse stock split was announced in the filing body | `reverse_split` | `bool(filing.get("full_text", {}).get("reverse_split"))` — non-null JSONB object means parser detected a ratio | ✅ **Match** — both fire iff the parser's `extract_reverse_split()` returned a dict |
| `form4_insider_buy` | See Section D | See Section D | See Section D | See Section D |
| `social_velocity_spike` | Social mention rate spike (Phase 1.5) | — (removed) | See Section C | N/A — removed |
| `short_interest_high` | SI% > 20 (Phase 2 / Ortex) | — (removed) | See Section C | N/A — removed |

**Concept-match risk #1:** `ir_firm_known_promoter` requires a NEW caller-side lookup with a `type='ir_firm'` filter. The temptation to wire it to FV-v1's existing `has_known_promoter_match` would silently broaden the signal. This PR explicitly does NOT take that shortcut; the new lookup is part of `evaluate_edgar_filing`'s `ticker_metadata` assembly.

**Concept-match risk #2 (low):** `underwriter_flagged` requires confirming the `underwriters.manipulation_flagged` column is actually populated in production. The column exists; whether any row has `manipulation_flagged=TRUE` today is a data question, not a schema one. Worth checking pre-merge with `SELECT COUNT(*) FROM underwriters WHERE manipulation_flagged=TRUE;` — if zero, the weight contributes nothing today but lights up as the manipulation-flag list is populated, which is the intended Phase 1 progression.

---

## C. Inert weight removal

**DECISION LOCKED.** Drop both weights from `_RULES_V1_WEIGHTS` in this PR. They re-enter with semantic review when the upstream extractors land — keeping them as schema-stable zero-weight placeholders would obscure the fact that the supporting infrastructure doesn't exist yet.

| Removed weight | Reason | Re-entry plan |
|---|---|---|
| `social_velocity_spike` (0.10) | Phase 1.5 — Telegram/Reddit ingestion not yet wired. The weight's been inert since the scorer was written | Re-enters when `social_signals` extraction lands. Semantic review at that point: define what "spike" means quantitatively (z-score? percent-over-baseline?) and confirm the weight value still makes sense in the calibrated context |
| `short_interest_high` (0.05) | Phase 2 — Ortex / SI data not in the pipeline; requires the data-tier upgrade per README | Re-enters with the Ortex integration. Semantic review at that point: confirm the SI% > 20 threshold is the calibrated boundary |

**Total weight removed: 0.15.** Sum of remaining weights post-removal:
`0.20 + 0.15 + 0.20 + 0.15 + 0.05 + 0.10 = 0.85`

Maximum possible score with current calibration: 0.85. (Pre-removal it was 1.00 if all eight signals fired.) The `_clamp_unit` floor still bounds the output to `[0, 1]`. Calibration is empirical anyway — these weights are placeholders per `RulesV1Scorer` docstring — so the maximum-attainable being 0.85 instead of 1.00 doesn't change correctness, only theoretical headroom. Documented for honesty.

---

## D. Form4 buy semantics

**DECISION LOCKED: P-only.** FV-v2's `is_form4_buy` fires on P-codes only, matching the original scorer comment and preserving its calibration intent. A-codes (grant/award acceptance) are NOT folded in. This narrows FV-v1's broader `is_form4_buy` (which accepted P+A) — that narrowing is exactly what the FV-v1 → FV-v2 schema bump exists to permit. Old FV-v1 predictions remain valid under their original schema; FV-v2 predictions use the narrower interpretation.

| Source | What fires | Codes |
|---|---|---|
| Old scorer comment (`catalyst_scorer.py:87`) | "Form 4 P-code transaction" | P only |
| FV-v1 extractor (`edgar_features.py:24`) | `_FORM4_BUY_CODES = frozenset({"P", "A"})` | P + A |
| FV-v2 extractor (this PR) | `_FORM4_BUY_CODES = frozenset({"P"})` | **P only** |
| `sec_filings.form4_insider_buy` boolean (parser) | What the parser writes when it sees a buy | Currently broader — see note below |

**Implementation note:** the parser-stored `sec_filings.form4_insider_buy` boolean is set by the existing Form-4 path without P/A discrimination. To make FV-v2's P-only contract honest, the extractor MUST read the explicit `form4_transaction_code` field from the filing dict (which `_build_signal_payload` exposes — currently `None` because the parser doesn't yet populate it). The fallback path through the legacy `form4_insider_buy` boolean is removed in FV-v2 — without an explicit code, the flag is False. This means the flag fires only when the Form-4 extractor is enhanced to write the transaction code; until then, FV-v2 form-4 predictions land at score 0 for that signal. Acceptable: it's calibration-honest. The Form-4 extractor enhancement is its own follow-up PR.

**Reasoning:** P (open-market purchase) signals more conviction than A (grant/award acceptance) — that distinction is the calibration intent the original scorer comment captured. Folding A-codes in (as FV-v1 did) silently broadens the signal and miscalibrates the weight. P-only matches the placeholder weight that's in `_RULES_V1_WEIGHTS` today.

---

## E. Weight preservation

**Confirmed: this PR changes ONLY the keys the scorer reads and the FV-v2 extractor output.** Specifically:

- ✅ `_RULES_V1_WEIGHTS` keys are renamed to FV-v2 vocabulary; **values are unchanged** (except the two removals in Section C, which drop 0.15 of theoretical headroom)
- ✅ `_clamp_unit` logic untouched
- ✅ `ScoreResult` shape untouched
- ✅ Prediction-worthy filter (`signals/filters/edgar_prediction_filter.py`) untouched
- ✅ `is_prediction_worthy` thresholds untouched
- ✅ `SIGNAL_TYPE_DEFAULTS` (window/target_pct) untouched
- ✅ `evaluate_edgar_filing` changes ONLY in `ticker_metadata` assembly (adding two new lookups for `ir_firm_known_promoter` and `underwriter_flagged`); the scoring call shape stays identical

**Section B's `ir_firm_known_promoter` narrowing and Section D's form-4 broadening are concept-shift items** — flagged per spec. Neither requires a weight change in this PR; both are honest reproductions of what the original scorer's keys meant. If reviewer disagrees on either, that's the scope-expansion signal — stop and discuss before code.

---

## F. Test plan

| Test | Status | What it pins |
|---|---|---|
| `test_diagnosis_extractor_and_scorer_share_zero_keys` | **Flip** — overlap will become non-empty | Diagnosis-as-pinned-test. Replace `assert overlap == set()` with `assert overlap >= {expected FV-v2 keys}` |
| `test_extractor_to_scorer_produces_nonzero_for_obvious_dilution_signal` | **Un-xfail** | Strict-xfail today; will start passing post-fix. CI fails loud if it doesn't, which is the intended trigger to remove the marker |
| `test_form4_buy_signal_reaches_scorer` | **Un-xfail** | Same pattern. The Section D broadening means this stays valid as written |
| **NEW** `test_extract_edgar_features_v2_emits_all_v2_keys` | New | Pins FV-v2 contract: every key in the new `FEATURE_KEYS_FV_V2` tuple appears in extractor output |
| **NEW** `test_rules_v1_scores_all_loaded_filing_above_threshold` | New | End-to-end integration: a fixture filing with all five new flags TRUE produces `probability ≈ sum(remaining_weights) = 0.85` (with `_clamp_unit` cap) |
| **NEW** `test_evaluate_edgar_filing_assembles_v2_metadata` | New | Pins that `evaluate_edgar_filing` populates `ir_firm_known_promoter` and `underwriter_flagged` in `ticker_metadata` before calling the extractor |
| Existing `test_score_result_probability_is_in_unit_interval_for_all_features` | **Update** | Test's hand-built feature dict uses the OLD vocabulary. Update to FV-v2 keys |
| Existing `test_score_handles_truthy_falsy_values_consistently` | **Update** | Same — hand-built dict needs the FV-v2 key set |
| Existing FV-v1 tests in `test_edgar_features.py` | **Add to, don't replace** | FV-v1 keys remain in FV-v2 (FV-v2 is a superset). Add new assertions for the five new keys; old assertions still pass |

CI gate: full suite green plus the two un-xfailed tests now passing strict.

---

## G. Schema version handling

`config/constants.py:104` `FEATURE_SCHEMA_VERSION` bumps `"fv-v1"` → `"fv-v2"`.

Add new `FEATURE_KEYS_FV_V2` tuple in `signals/features/edgar_features.py` (FV-v1 + the five new keys). Keep `FEATURE_KEYS_FV_V1` in place for historical reference and so the test suite can assert FV-v2 ⊇ FV-v1.

**Existing predictions:** ARTL, TVRD, KIDZ rows have `feature_schema_version='fv-v1'`. Per CLAUDE.md they're immutable. They become **naturally excluded** from Phase 1b calibration analysis once the analysis filters by `feature_schema_version='fv-v2'`. No backfill, no cleanup row, no separate migration step. This is the schema-version contract working as designed.

The three pre-fix predictions remain in the table as historical artifacts of the bug, query-able by anyone who wants to see the regression's footprint.

---

## Open items needing reviewer call

**All three resolved** — see Sections B, C, D above. Implementation proceeds.

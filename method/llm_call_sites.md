# LLM call sites

Implementation reference: `trace_run/trace_repair_b.py`; paper §3.6 (call accounting) and §3.7
(division of responsibility). Prompts are released verbatim in `prompts/`.

---

## 1. The six first-round calls

Order is fixed; the count is `6` before any revision.

| # | Call | Prompt file | Input | Output | Prohibited |
|---|---|---|---|---|---|
| 1 | Requirement extraction | `prompts/requirement_extraction.md` | Issue text | Requirement nodes and directed edges | Conditions absent from the issue |
| 2 | Edge refinement | `prompts/edge_optimization.md` | Issue + the graph from call 1 (node list, edge list) | Refined requirement edge set | New node definitions |
| 3 | Patch generation | `prompts/patch_generation.md` | Issue + target file content + requirement graph as JSON | Single-file unified diff | Cross-file edits, non-diff output |
| 4 | Implementation extraction | `prompts/implementation_graph.md` | Issue + candidate patch | Implementation nodes and directed edges | Symbols outside the patch |
| 5 | Traceability analysis | `prompts/traceability_analysis.md` | Requirement graph + implementation graph (+ computed coverage/clue block when enabled) | Free-form analysis prose | Declaring coverage |
| 6 | Structured linking | `prompts/structured_mapping.md` | The analysis from call 5 + both graphs | Requirement-edge ⇄ implementation-endpoint links (`M_t`) | Non-existent endpoints |

Failure handling, per the paper's table:

- Call 1: unparsable output is excluded from coverage.
- Call 2: only the parsable part of the edge set is kept.
- Call 3: no parsable diff is recorded as "no patch produced".
- Call 4: an empty result makes every requirement edge `NO_NODE`.
- Call 5: the text is retained but never used to compute coverage.
- Call 6: no valid `MAPPING:` line means an empty mapping.

## 2. The two calls added by each revision

| # | Call | Prompt file | Input | Output | Prohibited |
|---|---|---|---|---|---|
| 7 | Failure analysis | `prompts/failure_analysis.md` | Failure log + coverage results (`cov_t`, `U_t`) + implementation graph + `M_t` | The implicated implementation node or edge, plus what may be edited | Expanding the edit scope |
| 8 | Patch regeneration | `prompts/patch_generation.md`, third template | Issue + target file + previous candidate + failure log + the analysis from call 7 | A new single-file unified diff | Cross-file edits, non-diff output |

So `N_LLM = 6 + 2r` with `0 ≤ r ≤ R`, and `R = 1` in the reported experiments:

| Scenario | Model calls |
|---|---:|
| Initial candidate passes | 6 |
| Exactly one revision | 8 |

Deterministic graph processing, the four-state coverage computation, clue ranking, witness-path
computation, diffs parsing and test execution are **not** model calls.

> The `6 + 2r` accounting describes a revision that reuses `G_c^0` and `M_0`. A variant that
> re-extracts graphs inside the revision performs more calls per revision; see
> `method/iteration_and_termination.md` §4-5 for the recorded discrepancy.

## 3. Division of responsibility

The model performs semantics: turning the issue into requirement entities and relations, refining
the requirement edge set, generating the candidate diff, extracting implementation relations,
two-stage traceability, and failure analysis.

The program performs verdicts: coverage states, `U_t`, witness paths, graph normalisation, mapping
parsing, patch parsing, test evaluation, and the resolved/unresolved decision. Concretely, the
model may propose a correspondence between differently named entities, but that proposal cannot
turn `NO_NODE` into `DIRECT` or `PATH`; only the deterministic rule in
`method/coverage_states.md` decides (`M_t` does not alter `cov_t`).

Two properties follow from the split and are what the artifact is meant to make checkable:

- **Replaceability.** Changing the backbone model changes extraction and generation quality, but
  the four-state rule, the resolved criterion and the reporting conventions do not move.
- **Auditability.** Each stage records its graph, coverage result, mapping and patch by candidate
  index, so any reported state can be traced back to its endpoints and witness path.

## 4. Model routing in the shipped code

| Stage | Model in the shipped code |
|---|---|
| Patch generation / revision generation | `gpt-5.4` (explicit at the call site) |
| Requirement extraction, edge refinement, implementation extraction, traceability (×2), failure analysis | `gpt-5.2` (function default) |

Credentials and endpoint come from the environment (`TRACE_API_KEY`, `TRACE_BASE_URL`); nothing is
hard-coded, and no key is released with this artifact. The paper reports a GPT-5.5 configuration
(62.33%) and a GPT-5.4 configuration (52.00%); the retained records do not pin a per-call model
snapshot, which is one reason the numbers are not exactly reproducible today (see README,
"Notes on reproducibility").

## 5. Prompt constraints that support parsing

These are the structural constraints a reviewer can check line by line in `prompts/`:

- Separate `<issue>`, `<file_content>` and `<auxiliary_info>` tags; the auxiliary block carries only
  the requirement graph's structured JSON, never a prose summary.
- Diff output only, inside a fenced block, with `--- a/<path>` / `+++ b/<path>` headers matching the
  target file — full-file rewrites are excluded by construction.
- An AST-derived skeleton of public class and function names bounds the context length.
- Traceability is two-stage (free text, then structured links), so prose explanations never become
  computable mappings.
- Traceability and revision prompts carry the program-computed four-state labels and ranked clues,
  explicitly marked as computed by graph algorithms rather than judged by the model.
- The failure-analysis prompt must return exactly one `ERROR_NODE:` or `ERROR_EDGE:` line drawn
  from the supplied lists, and is forbidden from returning nothing.

These constraints are engineering, not a novel prompting technique — the paper says so explicitly
(§3.7); they exist so that the program can keep the verdict.

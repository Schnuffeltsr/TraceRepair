# TRACE-REPAIR Artifact

This repository contains the artifact package for the paper:

> **Traceability in the Generation Loop: Relational Modeling and Feedback Revision for Repository-Level Code Generation**

The artifact includes the task lists used in the evaluation, the prompt templates of every
LLM-based stage as they appear in the implementation, the deterministic coverage and ranking rules,
and the per-research-question result material (tables and figures) that supports the numbers
reported in the paper.

This repository is intended to help reviewers inspect and partially reproduce:

- which tasks were evaluated, and how the 300 / 60 / 20-task samples relate to each other;
- what each LLM stage is asked to do, verbatim, with its inputs, outputs and prohibitions;
- how a requirement relation is judged `DIRECT` / `PATH` / `UNCOVERED` / `NO_NODE` by deterministic
  rules, and what witness path and clue order are attached to that judgement;
- how the round budget, the candidate sequence and the termination conditions are defined;
- how the RQ1 / RQ2 / RQ3 numbers reported in the paper map onto the released material.

> **Note:** This artifact is primarily intended for inspection and reproduction of the reported
> experimental materials. Because the method relies on hosted LLM APIs, exact outputs may vary
> across time due to model updates, service-side changes, and decoding nondeterminism.

---

## 1. Repository Structure

```text
TraceRepair/
├── README.md
├── prompts/
│   ├── requirement_extraction.md
│   ├── edge_optimization.md
│   ├── patch_generation.md
│   ├── implementation_graph.md
│   ├── traceability_analysis.md
│   ├── structured_mapping.md
│   └── failure_analysis.md
├── data/
│   ├── swebench_lite_300_tasks.txt
│   ├── subset_20_tasks.txt
│   ├── subset_60_stratified.txt
│   └── human_eval_sample_50.txt
├── method/
│   ├── coverage_states.md
│   ├── iteration_and_termination.md
│   └── llm_call_sites.md
├── RQ1/
│   ├── rq1_main_results.csv
│   ├── rq1_ablation.csv
│   └── figures/
│       ├── fig_rq1_pass1.png
│       └── fig_rq1_pass1.pdf
├── RQ2/
│   ├── rq2_human_eval.csv
│   └── figures/
│       ├── fig_rq2_human.png
│       └── fig_rq2_human.pdf
├── RQ3/
│   ├── rq3_ablation.csv
│   └── figures/
│       ├── fig_rq3_ablation.png
│       └── fig_rq3_ablation.pdf
└── .gitignore
```

What is deliberately **not** included: the paper text itself, the full experiment harness, run
logs, caches, intermediate artifacts and backups. The artifact is the inspection material, not a
copy of the working tree the experiments were run from.

---

## 2. Artifact Contents

| Path | Purpose |
|---|---|
| `prompts/` | The seven prompt templates used by the pipeline, copied verbatim from the implementation, with the call site, inputs, outputs and prohibitions of each. |
| `data/` | Instance-level task lists: the 300-task SWE-bench Lite pool, the 60-task stratified sample, the 20-task official-harness re-check, and the status of the 50-task human-evaluation sample. |
| `method/` | The deterministic part of the method: four-state coverage with witness paths and clue ranking, the iteration / round-budget / termination rules, and the per-stage LLM call sites. |
| `RQ1/` | Pass@1 of the proposed method against five comparison configurations on the same 300 tasks, plus the ablation view of the same 300-task setting, and the RQ1 figure. |
| `RQ2/` | Human-evaluation rates for requirement edges, candidate implementation edges and cross-graph mappings, plus the RQ2 figure. |
| `RQ3/` | Component-ablation results, the extension configurations with their negative results, and the RQ3 figure. |

---

## 3. How to Inspect the Artifact

### Step 1: Check the prompt templates

```text
prompts/
```

Each file starts with the source location (file and line range in the implementation), the call
site in the accounting `N_LLM = 6 + 2r`, the input, the output and the prohibited content. The
template itself follows in a fenced block and is a byte-for-byte slice of the source — nothing is
paraphrased, re-indented or reworded. Where a prompt is a single-line f-string in the source, the
raw line is given first and a rendering with the escape sequences expanded is given second.

Two things are worth checking against the paper here:

- the separate `<issue>`, `<file_content>` and `<auxiliary_info>` tags, with the auxiliary block
  carrying only the requirement graph's structured JSON;
- the diff-only output constraint with headers that must match the target path, and the
  two-stage traceability (free-form analysis, then structured links) that keeps prose out of the
  computed mapping.

### Step 2: Check the task lists

```text
data/
```

| File | Content | Relation to the paper |
|---|---|---|
| `swebench_lite_300_tasks.txt` | 300 `instance_id`s of the SWE-bench Lite test split | The main experiment's task pool (§4.2, §4.3) |
| `subset_60_stratified.txt` | 60 instances, split into easy / medium / hard blocks of 20 | The stratified re-evaluation: 30/60 resolved (§4.2, §4.3) |
| `subset_20_tasks.txt` | 20 instances re-evaluated with the official images and parser | 20/20 patches applied, 11/20 full `FAIL_TO_PASS`, 9/20 resolved (§4.3.1) |
| `human_eval_sample_50.txt` | **status only** — the 50 ids were not preserved | The 50-task human evaluation of RQ2 (§4.4); see the file and §7 below |

Lines starting with `#` are comments; every other line is one `instance_id`. Comments carry the
provenance, the tier definition and the paper result for that sample. The 20-task and 60-task lists
are subsets of the 300-task list; 15 of the 20 tasks also appear in the 60-task sample, and the
files say so where it matters for a paired comparison.

### Step 3: Check the deterministic rules

```text
method/
```

- `coverage_states.md` — the four states and the order in which they are tested, the shortest
  witness path for `PATH`, the uncovered set `U_t`, the clue ranking with its tie-breaks, the exact
  text of the block that is injected into the prompts, and a worked example.
- `iteration_and_termination.md` — the candidate sequence, `R = 1`, what triggers a revision, the
  objective, which state is reused across revisions, and the termination conditions in program
  order.
- `llm_call_sites.md` — the six first-round calls and the two calls added per revision, each with
  input, output and prohibitions, plus the division of responsibility between model and program.

The single most checkable property of the method is here: **the model never assigns a coverage
state.** Endpoint existence, direct adjacency and reachability decide the state; the model's
cross-graph mapping may explain a gap but cannot change it.

### Step 4: Check the RQ material

| Folder | Corresponding research question | Main purpose |
|---|---|---|
| `RQ1/` | RQ1 — task-solving effectiveness | Pass@1 on the same 300 tasks against five comparison configurations, plus the ablation view |
| `RQ2/` | RQ2 — trustworthiness of intermediate artifacts | Human evaluation of requirement edges, implementation edges and cross-graph mappings |
| `RQ3/` | RQ3 — component contributions | Component ablations, and the extension configurations whose results were negative |

Each CSV carries a `source` or `note` column naming the paper location and, where the paper reports
only a percentage, saying so. Figures are provided as both `.png` (inspection) and `.pdf` (vector,
print).

`RQ1/rq1_ablation.csv` and `RQ3/rq3_ablation.csv` both concern ablations; they differ in view.
`RQ1/rq1_ablation.csv` restates the three ablated configurations in the RQ1 table format (Pass@1 and
delta on the same 300 tasks) because the ablation is run in the same setting as the main
experiment. `RQ3/rq3_ablation.csv` is the RQ3 analysis view: the values plotted in
`fig_rq3_ablation`, plus the extension configurations of §4.6.1 with their discordant-pair ratios
(`OR = 11.0` and `OR = 30.0`), p-values and resolved counts.

### Step 5: Trace a reported number

Every headline number in the paper is reported once in this artifact:

| Paper claim | Value | Where |
|---|---|---|
| Main experiment, TRACE-REPAIR (GPT-5.5) | 62.33% (187/300) | `RQ1/rq1_main_results.csv` |
| Direct Repair | 52.33% | `RQ1/rq1_main_results.csv` |
| Text-Only Self-Refine (budget-matched) | 55.00% | `RQ1/rq1_main_results.csv` |
| TRACE-REPAIR (GPT-5.4) | 52.00% | `RQ1/rq1_main_results.csv` |
| SOEN / GitHub Baseline / SCoT | 31.67% / 30.00% / 13.00% | `RQ1/rq1_main_results.csv` |
| Stratified 60-task re-evaluation | 30/60 | `RQ1/rq1_main_results.csv`, `data/subset_60_stratified.txt` |
| 20-task official re-check | 20/20 applied, 11/20 F2P, 9/20 resolved | `RQ1/rq1_main_results.csv`, `data/subset_20_tasks.txt` |
| Issue-Edge Validity | 93.18% (410/440) | `RQ2/rq2_human_eval.csv` |
| Patch-Edge Support | 89.42% (465/520) | `RQ2/rq2_human_eval.csv` |
| Cross-graph mapping, strict / lenient / wrong-or-weak | 36.62% / 78.87% / 21.13% (130, 280, 75 of 355) | `RQ2/rq2_human_eval.csv` |
| Ablation: full / w/o issue graph / w/o patch graph / w/o mapping | 62.33 / 60.00 / 59.67 / 56.00 (%) | `RQ3/rq3_ablation.csv`, `RQ1/rq1_ablation.csv` |
| Deterministic graph processing, enabled vs control | 9/20 vs 6/20, p = 0.25 | `RQ3/rq3_ablation.csv` |
| Test-driven localization, replacement | 22/43 vs 4/43, OR = 11.0, p < 0.001 | `RQ3/rq3_ablation.csv` |
| Test-driven localization, union | 30/55 vs 10/55, OR = 30.0, p < 0.001 | `RQ3/rq3_ablation.csv` |

Following the paper's convention, an em dash (`—`) means *not separately reported in the original
records*; it never means zero. Task counts are given wherever the records carry them, and left as
`—` where the paper reports only a percentage.

---

## 4. Mapping Between Paper Sections and Artifact Material

| Paper component | Artifact location |
|---|---|
| §3.2 Requirement-side relation modeling (extraction, refinement, optional normalisation) | `prompts/requirement_extraction.md`, `prompts/edge_optimization.md`, `method/coverage_states.md` §1 |
| §3.3 Implementation-side relation modeling (patch generation, implementation graph) | `prompts/patch_generation.md`, `prompts/implementation_graph.md` |
| §3.4 Coverage determination, gaps, witness path, clue ranking | `method/coverage_states.md` |
| §3.5 Cross-graph traceability mapping | `prompts/traceability_analysis.md`, `prompts/structured_mapping.md` |
| §3.6 Failure-driven revision and round budget | `method/iteration_and_termination.md`, `prompts/failure_analysis.md` |
| §3.7 LLM calls and division of responsibility | `method/llm_call_sites.md` |
| §4.2 Experimental setup (300 tasks, 60-task stratified sample, resolving criterion) | `data/`, `RQ1/rq1_main_results.csv` |
| §4.3 RQ1 effectiveness | `RQ1/` |
| §4.4 RQ2 human evaluation | `RQ2/`, `data/human_eval_sample_50.txt` |
| §4.5 RQ3 component contributions | `RQ3/` |
| §4.6.1 Extension configurations and negative results | `RQ3/rq3_ablation.csv` |

---

## 5. Notes on Reproducibility

This artifact supports inspection and partial reproduction, not bit-exact re-execution. Because
TRACE-REPAIR calls hosted LLM APIs, exact generated outputs may change over time with model
updates, service-side changes and decoding nondeterminism. To mitigate this, the repository
provides:

- the instance-level task lists, so the evaluated tasks are known;
- the prompt templates verbatim, so each stage's request is known;
- the deterministic rules, so every coverage verdict is recomputable from a graph and a mapping;
- the per-RQ tables and figures with the paper location of every number.

If exact numerical reproduction is required, a reviewer should also document: the LLM provider and
model snapshot per stage; decoding parameters such as temperature and top-p; the API date; the
prompt template version; the patch post-processing and filtering rules; and the evaluator
configuration. The shipped code routes patch generation to `gpt-5.4` and the graph, traceability and
failure calls to `gpt-5.2` (`method/llm_call_sites.md` §4); the paper reports a GPT-5.5 and a
GPT-5.4 configuration, and the retained records do not pin a per-call model snapshot.

Evaluation uses the official SWE-bench images and parser throughout: a task is resolved when all
`FAIL_TO_PASS` tests pass and no `PASS_TO_PASS` test regresses. All denominators are
intention-to-treat — tasks are not dropped because a run failed or a record is missing.

---

## 6. Known Gaps and TODO

1. **The 50 human-evaluation tasks are not identified.** `data/human_eval_sample_50.txt` states this
   explicitly and deliberately contains no ids. The paper and the internal notes describe the sample
   only as "a random sample of 50 tasks" from the 300-task pool; no seed, id list or per-task
   annotation table for those 50 tasks survives in the project records. The announced rates in
   `RQ2/rq2_human_eval.csv` are unaffected — only the identity of the sampled tasks is missing.
   Releasing the 50 ids (and the per-relation annotation table) remains open.
2. **Annotator metadata is missing.** The paper reports this limitation itself: the number of
   annotators, the disagreement handling and an agreement statistic were not recorded, which is why
   the strict and lenient criteria are reported separately rather than merged.
3. **Graph reuse during revision.** The paper states that the implementation graph and the mapping
   are computed for the initial candidate and reused during revisions (§3.3, §3.6), while its
   introduction describes them as rebuilt each round, and the variant shipped in the experimental
   tree re-extracts them inside the retry block. This artifact documents the method as defined in
   §3.3/§3.6 and releases the templates verbatim; the discrepancy is recorded in
   `method/iteration_and_termination.md` §4 and is not resolved here.
4. **Exact-name matching.** Coverage uses endpoint names, so a requirement node and its
   implementation counterpart spelled differently yield `NO_NODE`. A variant with a deterministic
   alias map exists in the experimental tree; the paper defines the rules without it. See
   `method/coverage_states.md` §8.
5. **Per-task run records.** The per-task graphs, mappings, patches and evaluation verdicts behind
   the reported numbers are not released here. Only the aggregate tables and the sample-level task
   lists are.

---

## 7. Citation

```bibtex
@article{tracerepair2026,
  title  = {Traceability in the Generation Loop: Relational Modeling and Feedback Revision for
            Repository-Level Code Generation},
  author = {Anonymous},
  year   = {2026},
  note   = {Submitted for review}
}
```

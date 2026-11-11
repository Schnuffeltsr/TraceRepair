# Iteration, round budget and termination

Implementation reference: `trace_run/trace_repair_b.py` (`main()`, retry loop and
`TRACE_MAX_RETRIES`); paper §3.6 (revision and round budget), §3.7 (call accounting).

---

## 1. Candidate sequence

The loop produces one initial candidate and at most `R` revisions:

```
Δ_0, Δ_1, …, Δ_T          0 ≤ T ≤ R
N_candidate ≤ 1 + R
```

- `Δ_0` comes from the patch-generation call conditioned on the requirement graph.
- `Δ_t (t ≥ 1)` comes from the revision call, conditioned on the previous candidate, the failure
  log and the failure analysis.
- Every candidate stays restricted to the single target file.

**`R = 1` in all reported experiments.** The implementation parameter is `max_retries`, read from
the environment as `TRACE_MAX_RETRIES` with default `1`; the code supports larger budgets but no
reported result uses one. With `R = 1` a task sees at most two candidates: the initial attempt plus
at most one revision.

## 2. What triggers a revision

```
task terminates if evaluation passes
otherwise: revision starts if retry budget remains
```

- The trigger is **behavioural failure** (a failing evaluation of the current candidate), not a
  non-empty `U_t`. A structurally uncovered relation set alone never starts a revision, because
  `U_t` is evidence about structure and the tests are evidence about behaviour (paper §3.4, §3.6).
- The task ends as *resolved* only when the judging chain passes: all `FAIL_TO_PASS` tests pass
  **and** no `PASS_TO_PASS` test regresses (paper §4.2).
- Candidates that do not parse as a diff, or whose diff cannot be applied, end the attempt for that
  candidate; if no revision budget remains, the task is counted as unresolved under the
  intention-to-treat denominator — tasks are never dropped because a run failed (paper §4.2).

## 3. Objective

The method aims at a candidate sequence that empties the structural gap set while satisfying the
behavioural criterion:

```
min_{Δ_0,…,Δ_T} |U_R|
subject to  0 ≤ T ≤ R,  Tests(Δ_T) = PASS,  P2P(Δ_T) = PASS
```

- If the process terminates early (tests pass at `t = T < R`), `U_R := U_T`.
- This is a statement of intent, not a claim that the implementation globally optimises the
  candidate space: candidates are revised sequentially from failure feedback, and the loop stops at
  the first behavioural pass. `U_R` is retained as evidence used for prompting and traceability, not
  as a newly measured structural optimum after revision.

## 4. Graph and mapping reuse across revisions

```
G_c^t = G_c^0,  M_t = M_0,  cov_t = cov_0,  U_t = U_0        1 ≤ t ≤ R
```

Graph extraction and cross-graph mapping are computed for the initial candidate and inherited by
the revision, so re-evaluation can show a behavioural change but cannot show that the system
re-measured a reduction of structural gaps. This is why the paper reports `U_R` as guidance
evidence rather than as an outcome measurement.

> **Discrepancy recorded, not resolved.** The paper states that revisions do not re-extract the
> implementation graph or the mapping. The shipped variant in `trace_run/` (the "B" configuration,
> `trace_repair_b.py`, which is also the file the prompt templates in `prompts/` are taken from)
> re-runs implementation-graph extraction, traceability analysis and structured mapping inside its
> retry block. The retained records do not allow us to attribute the reported numbers to one of the
> two behaviours, so this artifact documents the method as defined in the paper and releases the
> templates verbatim; the difference is visible in `trace_repair_b.py` itself and is left for the
> owner to reconcile.

## 5. Call accounting

```
N_LLM = 6 + 2r,   0 ≤ r ≤ R
```

| Round | Calls | Total |
|---|---:|---:|
| Initial candidate, evaluation passes | 6 | 6 |
| One revision (`r = 1`) | +2 | 8 |

Deterministic graph processing, coverage computation, and test execution are not model calls.
Individual call sites, inputs, outputs and prohibitions are listed in
`method/llm_call_sites.md`.

> The same caveat as in §4 applies to the count: the paper's `6 + 2r` counts graph extraction and
> mapping once. A variant that re-extracts inside the revision adds calls per revision, so the
> realised call count of such a run is higher than `8`. The figure of `6` for a first-round pass
> holds in both variants.

## 6. Termination conditions, in the order the program checks them

1. **Test pass** → terminate as resolved, `U_R := U_T`.
2. **Test failure with budget left** (`r < R`) → run failure analysis, regenerate the diff,
   re-evaluate.
3. **Test failure without budget left** → terminate as unresolved.
4. **No parsable patch produced** → the attempt ends; with no budget left the task is unresolved.
   (In the shipped variant an unusable candidate breaks out of the retry loop instead of consuming
   the remaining budget.)
5. **Infrastructure failure** (image not available, evaluation error) → recorded as an evaluation
   error and kept in the intention-to-treat denominator rather than silently dropped.

Every terminating path records a verdict plus the artifacts of the last round (graph, coverage,
mapping, patch, evaluation result), so a reported number can be traced back to a specific candidate
index.

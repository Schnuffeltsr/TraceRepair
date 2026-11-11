# Code

This directory contains the implementation used for the reported experiments, split by research
question. Shared modules live in `common/` and are imported by the per-RQ entry points.

All scripts are Python 3.12. Model access is configured exclusively through environment variables,
so no credential appears in the code:

```text
TRACE_BASE_URL   OpenAI-compatible endpoint
TRACE_MODEL      model name
TRACE_API_KEY    API key
```

Docker is required for evaluation, because the tests run on the official SWE-bench images.

## common/

| File | Role |
|---|---|
| `graph_refine.py` | Deterministic graph processing and coverage: the four-state decision (`DIRECT` / `PATH` / `UNCOVERED` / `NO_NODE`), shortest witness path, clue ranking, and the normalisation applied to extracted graphs (self-loops, cycles, transitive reduction). No LLM calls. |
| `repograph.py` | Repository-structure graph: file-level import relations and a symbol index, built from the AST. Deterministic and cacheable. |
| `testloc.py` | Test-driven file localisation: recovers the implementation file from the FAIL_TO_PASS test identifiers. Deterministic, no LLM. Used by the localisation experiments. |
| `multifile_min.py` | Minimal multi-file support (pure functions): candidate file selection, multi-file diff prompt assembly, reply parsing. Used by the multi-file variant. |

## rq1/ — main experiment, 300 tasks

| File | Role |
|---|---|
| `trace_repair_b.py` | The pipeline evaluated in RQ1. Six model calls on the first candidate (requirement extraction, edge optimisation, patch generation, implementation-graph extraction, free-form traceability analysis, structured mapping) plus two per revision (failure analysis, patch regeneration). The implementation graph and the mapping are extracted on the initial candidate and reused during revision; `TRACE_GRAPH_REFINE=1` enables the deterministic graph processing in `common/graph_refine.py`. |
| `batch_experiment.py` | Batch driver: runs one variant over a task list inside Docker, with per-instance repositories, timeout handling and result collection. |
| `make_final_report.py` | Aggregates per-instance results and the paired analysis into the reported tables. |

Entry point:

```text
python rq1/batch_experiment.py --variant b --tasks data/swebench_lite_300_tasks.txt
```

## rq2/ — human evaluation

Manual annotation only; no automation produced the reported percentages. `README.md` in that
directory documents the annotation unit and denominators. The aggregated counts are in
`RQ2/rq2_human_eval.csv`.

## rq3/ — component contribution and negative results

| File | Role |
|---|---|
| `make_variant_b.py` | Builds the graph-processing variant from the working copy by inserting the deterministic layer behind `TRACE_GRAPH_REFINE`. The original file is never modified. |
| `make_variant_mf.py` | Builds the multi-file variant, wiring `common/multifile_min.py` into the pipeline. |
| `analyze_pairing.py` | Paired analysis of two result sets: per-task agreement table, discordant counts, and failure attribution. |

Entry point:

```text
python rq3/analyze_pairing.py --runs runs
```

## Note on variants

`make_variant_*.py` generate variant files rather than shipping them, so the pipeline that produced
each reported number is reproducible from a single source. A further experimental variant
re-extracts the implementation graph and the mapping at every revision; it issues more model calls
than the configuration described in the paper, and the reported results do not use it.

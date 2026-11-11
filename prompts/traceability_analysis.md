# Prompt 5 — Traceability analysis (free-text cross-graph analysis)

**Source:** `trace_run/trace_repair_b.py`, `trace_analysis_prompt` inside `main()`
(line 1840). **Verbatim, unmodified.**

**Call site:** the fifth of the six first-round model calls (paper §3.5, §3.6).
**Input:** the issue text, the requirement graph as JSON, the candidate patch, the
implementation-graph edge text, and — when deterministic processing is enabled — the
`[Computed coverage of issue relations]` / `[Ranked inspection clues]` block produced by
`graph_refine.format_for_prompt()`. That block is marked as "computed by graph algorithms,
not judged by the model" (paper §3.7).
**Output:** free-form prose. It is stored as `trace_analysis` in the per-task result record and
is **not** used to compute coverage.
**Prohibited:** declaring coverage (paper §3.7 table). `cov_t` is computed by the program
only, and `M_t` cannot move an edge out of `NO_NODE` (paper §3.5).

The template is a single-line f-string in the source, so the raw line is shown first and a
de-escaped rendering of the same string is shown second for readability. The two contain
identical text; nothing was added or dropped.

## Raw template (line 1840, verbatim single-line f-string)

````text
                    trace_analysis_prompt = f"========== Issue 描述 ==========\n{prompt}\n========== 需求因果图 ==========\n{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}\n========== 生成的修复 Patch ==========\n{model_patch}\n========== 生成的代码因果图 ==========\n{code_graph_text}\n{('========== 计算结果（图算法，非模型判断） ==========\n' + computed_block + chr(10)) if computed_block else ''}========== 任务 ==========\n请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。"
````

## The same template with the escape sequences rendered (`\n` → line break)

````text
========== Issue 描述 ==========
{prompt}
========== 需求因果图 ==========
{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}
========== 生成的修复 Patch ==========
{model_patch}
========== 生成的代码因果图 ==========
{code_graph_text}
========== 计算结果（图算法，非模型判断） ==========
{computed_block}
========== 任务 ==========
请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。
````

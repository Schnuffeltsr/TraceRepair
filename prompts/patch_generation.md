# Prompt 3 — Patch generation (requirement graph → single-file unified diff)

**Source:** `trace_run/trace_repair_b.py`, `build_diff_prompt()` (lines 1153-1176).
**Verbatim, unmodified.**

**Call site:** the third of the six first-round model calls (paper §3.3, §3.6).
**Input:** the issue text, the target file content at the base commit, and the requirement graph
as structured JSON in `<auxiliary_info>` (built in `main()`, lines 1761-1764:
`{"nodes": [...], "edges": ["u->v", ...]}`).
**Output:** one unified diff, wrapped in a fenced `diff` code block, headers matching `rel_path`.
**Prohibited:** cross-file edits and non-diff output (paper §3.7 table). Output that does not
parse as a diff counts as "no patch produced" (`generate_patch_with_llm()`, lines 1284-1311).

The issue, the file content and the auxiliary graph are placed in separate `<issue>`,
`<file_content>` and `<auxiliary_info>` tags, and the auxiliary block carries only the
structured JSON — never a prose summary (paper §3.7).

Two further templates live in the same function family and are included for completeness: a
full-file rewrite variant used when a diff cannot be produced, and the revision prompt used in
round `t ≥ 1`. The revision prompt is deliberately kept in the same file so that the
"initial vs. revision" difference is auditable.

## `build_diff_prompt()` — initial candidate (lines 1153-1176)

````text
    return f"""You are a Principal Software Engineer.
The file `{rel_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_path}">
{content}
</file_content>

<auxiliary_info>
Demand Causal Graph: {json.dumps(graph_info)}
</auxiliary_info>

## CRITICAL INSTRUCTION
The file may be large. Please fix the issue by providing a **Unified Diff**.
1. ONLY output the Unified Diff patch.
2. DO NOT output the entire file.
3. Wrap the diff in a ```diff ... ``` block.
4. Ensure the diff header matches the file path exactly:
--- a/{rel_path}
+++ b/{rel_path}
"""
````

## `build_full_file_prompt()` — full-file variant (lines 1126-1149)

````text
    return f"""You are a Principal Software Engineer.
The file `{rel_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_path}">
{content}
</file_content>

<auxiliary_info>
Demand Causal Graph: {json.dumps(graph_info)}
</auxiliary_info>

## CRITICAL INSTRUCTION
Please rewrite the **ENTIRE FILE** content of `{rel_path}` to fix the issue.
1. Output the FULL content of the file.
2. DO NOT omit any lines. DO NOT use `...` or `// rest of code`.
3. Wrap the content in a code block like:
{CODE_BLOCK_START}
... content ...
{CODE_BLOCK_END}
"""
````

## `generate_fixed_patch_with_llm()` — revision candidate (lines 1327-1361)

````text
    prompt = f"""You are a Principal Software Engineer.
The file `{rel_file_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_file_path}">
{file_content}
</file_content>

You previously generated this patch, but it failed the tests:
<previous_patch>
{previous_patch}
</previous_patch>

The test execution returned the following error log:
<test_error_log>
{error_log}
</test_error_log>

Based on our Causal Traceability Graph analysis, the error is likely caused by the following code element:
<error_graph_analysis>
{error_analysis}
</error_graph_analysis>

## CRITICAL INSTRUCTION
Please fix the issue and the test failures by providing a new **Unified Diff**.
1. ONLY output the Unified Diff patch.
2. DO NOT output the entire file.
3. Wrap the diff in a ```diff ... ``` block.
4. Ensure the diff header matches the file path exactly:
--- a/{rel_file_path}
+++ b/{rel_file_path}
"""
````

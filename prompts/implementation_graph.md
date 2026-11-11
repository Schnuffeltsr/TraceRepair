# Prompt 4 — Implementation-graph extraction (candidate patch → code causal graph)

**Source:** `trace_run/trace_repair_b.py`, `create_graph_only_prompt()` (lines 1181-1192) and
the parser `extract_code_graph()` (lines 1195-1203). **Verbatim, unmodified.**

**Call site:** the fourth of the six first-round model calls (paper §3.3, §3.6).
**Input:** the issue text and the candidate patch. In the shipped code the issue is passed as
the unused first parameter `prompt_text`; only the patch is interpolated into the template.
**Output:** implementation nodes and directed edges, one `NodeA -> NodeB [relation]` per line
inside the `=== BEGIN CODE CAUSAL GRAPH ===` fence, parsed by
`build_code_causal_graph_from_text()` (lines 1206-1219).
**Prohibited:** symbols outside the patch (paper §3.7 table). An empty or unparsable result
leaves the endpoint set empty, so every requirement edge is reported as `NO_NODE` (paper §3.4).

Graph extraction runs for the initial candidate. The subsequent deterministic processing
(`graph_refine.refine_graph`) is what the paper calls optional normalisation.

## `create_graph_only_prompt()` — the template (lines 1181-1192)

````text
Generate a "Code Causal Graph" based on this PATCH.

<patch>
{generated_patch}
</patch>

Format:
=== BEGIN CODE CAUSAL GRAPH ===
NodeA -> NodeB [calls]
...
=== END CODE CAUSAL GRAPH ===
"""
````

## `extract_code_graph()` — the parsing contract (lines 1195-1203)

````text
def extract_code_graph(response):
    graph_match = re.search(r'=== BEGIN CODE CAUSAL GRAPH ===(.*?)=== END CODE CAUSAL GRAPH ===', response, re.DOTALL)
    if graph_match:
        return graph_match.group(1).strip()
    lines = response.strip().split('\n')
    valid_lines = [l for l in lines if '->' in l or '→' in l]
    if len(valid_lines) > 0:
        return "\n".join(valid_lines)
    return ""
````

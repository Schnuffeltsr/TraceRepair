# Prompt 7 — Failure analysis (test log + coverage → implicated code element)

**Source:** `trace_run/trace_repair_b.py`, `analyze_error_and_highlight_graph()`
(lines 551-576). **Verbatim, unmodified.**

**Call site:** the first of the two calls added by each revision (`N_LLM = 6 + 2r`; the second
is the revision prompt in `prompts/patch_generation.md`). Paper §3.6.
**Input:** the failure log (with the computed coverage/clue block appended when deterministic
processing is enabled; `main()` line 1872), the implementation-graph node list, the
implementation-graph edge list, and the current `M_t` links.
**Output:** exactly one annotation, `ERROR_NODE: <node>` or `ERROR_EDGE: A -> B`, parsed at
lines 582-590.
**Prohibited:** expanding the edit scope beyond the implicated element (paper §3.7 table);
the prompt forces a choice from the supplied lists and forbids returning nothing.

The "you MUST pick one" instruction is deliberately absolute: the stage is a localisation
device, and an empty answer would make the revision prompt unanchored. The paper notes this
behavioral-failure-only trigger — a non-empty `U_t` alone never starts a revision (paper §3.6).

## The template (lines 551-576)

````text
    prompt = f"""
你是一个高级调试专家。当前代码测试失败，以下是测试报错信息以及因果追溯图的信息。
请根据测试报错，指出因果追溯图中哪个代码节点或代码边最可能导致了该报错。

[测试报错]
{error_log}

[代码节点]
{code_nodes}

[代码边]
{code_edges}

[追溯映射]
{trace_connections}

【强制要求 / CRITICAL INSTRUCTION】
既然测试已经失败，说明图中必定存在未成功实现或存在Bug的节点/边。
你**必须（MUST）**从上述给定的[代码节点]或[代码边]中，挑选出**最相关的一个**进行标注！绝对不允许不返回任何标注！
即使报错信息不够明确，你也必须基于逻辑推断给出一个最有可能的节点或边。

请只返回一个最相关的代码节点或代码边。
如果是节点，请严格按照格式输出：ERROR_NODE: 节点名
如果是边，请严格按照格式输出：ERROR_EDGE: 节点A -> 节点B
不要输出其他任何解释性文字。
"""
````

## Response parsing (lines 582-590, verbatim)

````text
    for line in reply.split('\n'):
        line = line.strip()
        if line.startswith("ERROR_NODE:"):
            error_node = line.replace("ERROR_NODE:", "").strip()
        elif line.startswith("ERROR_EDGE:"):
            edge_str = line.replace("ERROR_EDGE:", "").strip()
            if "->" in edge_str:
                u, v = edge_str.split("->")
                error_edge = (u.strip(), v.strip())
````

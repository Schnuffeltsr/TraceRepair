# Prompt 2 — Edge optimization (requirement-edge refinement)

**Source:** `trace_run/trace_repair_b.py`, `format_graph_info_for_llm()` (system prompt:
lines 1003-1015; user prompt: line 1016). **Verbatim, unmodified.**

**Call site:** the second of the six first-round model calls (paper §3.2, §3.6).
**Input:** the issue text plus the initial graph produced by Prompt 1 (node list and edge list,
rendered by the `user_prompt` f-string).
**Output:** the optimized edge list, one `source → target` per line, parsed by
`parse_llm_reply_for_edges()` (lines 1020-1026).
**Prohibited:** new node definitions — refinement may delete, add or redirect edges among the
nodes it was given, but the node set is not re-declared (paper §3.7 table).

After parsing, the pipeline removes nodes that participate in no edge (paper §3.2). Optional
deterministic normalisation (self-loop removal, weakest-edge cycle breaking, transitive
reduction) is implemented in `trace_run/graph_refine.py::refine_graph()`; it is a separate
switch and is not part of this prompt.

## System prompt

````text
    system_prompt = """你是一名因果图优化专家，需要根据编程题目的内容和语义来优化因果图。
分析要求：
1. 仔细阅读题目描述，理解其逻辑和要求
2. 分析图中的节点（关键词）之间的真实因果关系
3. 基于题目语义，判断边是否存在真实的因果或依赖关系
4. 删除伪相关、冗余或不符合题目逻辑的边
5. 可以添加题目中隐含但图中缺失的重要因果关系
6. 确保优化后的图能更好地反映题目要求的逻辑关系

输出格式要求：
请输出优化后的边列表，每行一条边，格式为：源节点 → 目标节点
如果认为应该删除所有边，可以输出"无相关边"。
"""
````

## User prompt (template; `problem_info`, `nodes_info`, `edges_list` are filled by the function)

````text
    user_prompt = f"========== 题目信息 ==========\n{problem_info}\n\n========== 初始因果图信息 (基于 GES 逻辑生成) ==========\n节点列表：\n{nodes_info}\n\n原始有向边：\n{'; '.join(edges_list) if edges_list else '暂无边'}\n\n========== 分析任务 ==========\n请基于以上题目内容和语义，优化这个因果图。\n请给出优化后的边列表："
````

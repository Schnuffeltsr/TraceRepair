# Prompt 6 — Structured mapping (free-text analysis → requirement-edge ⇄ implementation links)

**Source:** `trace_run/trace_repair_b.py`, `create_trace_graph_structure_prompt()`
(lines 885-920) and the parser `parse_trace_graph_structure()` (lines 923-956).
**Verbatim, unmodified.**

**Call site:** the sixth (last) of the six first-round model calls (paper §3.5, §3.6).
**Input:** the requirement-graph edge list, the implementation-graph node and edge lists, and
the free-text report from Prompt 5.
**Output:** `MAPPING:` lines, either `demand edge >>> code edge` or `demand edge >>> code node`,
parsed into `M_t ⊆ E_r × (V_c ∪ E_c)`.
**Prohibited:** non-existent endpoints (paper §3.7 table). A `MAPPING:` line whose endpoints are
not in the supplied lists is dropped by the parser, and no valid line means an empty mapping.

Note the two deliberate constraints on the model: it must copy node names exactly from the
input, and it may not emit explanatory text — both properties are what make `M_t` parseable at
all. The header/edge-list construction in the function body (lines 886-888) is included
because it fixes the exact textual form the model sees.

## Graph-to-text rendering used as input (lines 886-888, verbatim)

````text
    demand_edges_str = "\n".join([f"{u} -> {v}" for u, v in demand_graph.edges()])
    code_edges_str = "\n".join([f"{u} -> {v}" for u, v in code_graph.edges()])
    code_nodes_str = ", ".join(list(code_graph.nodes()))
````

## `create_trace_graph_structure_prompt()` — the template (lines 890-920)

````text
    return f"""
    你是一个专业的数据可视化架构师。
    你的任务是根据“需求因果图”、“代码因果图”和“因果追溯报告”，构建    一个精确的“因果追溯图”结构数据。

    ========== 输入数据 ==========
    [需求因果图边]:
    {demand_edges_str}

    [代码因果图节点]: {code_nodes_str}
    [代码因果图边]:
    {code_edges_str}

    [因果追溯报告]:
    {trace_report}

    ========== 输出要求 ==========
    请输出一个结构化的列表，明确指出每一条需求边（Demand Edge）对应到了代码中的哪个元素（代码节点或代码边）。

    请严格使用以下格式（MAPPING: 需求边 >>> 代码元素）：

    格式 1 (对应代码边):
    MAPPING: DemandNodeA -> DemandNodeB >>> CodeFuncA -> CodeFuncB

    格式 2 (对应代码节点):
    MAPPING: DemandNodeA -> DemandNodeB >>> CodeFuncNode

    注意：
    1. 请确保节点名称与输入数据完全一致。
    2. 不要输出任何解释性文字，只输出 MAPPING 行。
    3. 如果有多个代码实现，请选择最核心的一个。
    """
````

## `parse_trace_graph_structure()` — the parsing contract (lines 923-956)

````text
def parse_trace_graph_structure(response_text):
    connections = []
    lines = response_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line.startswith("MAPPING:"): continue
        content = line.replace("MAPPING:", "").strip()
        if ">>>" not in content: continue
        parts = content.split(">>>")
        if len(parts) != 2: continue

        left_part = parts[0].strip()
        right_part = parts[1].strip()

        left_part = re.sub(r'DEMAND_EDGE\((.*?)\)', r'\1', left_part)
        right_part = re.sub(r'CODE_EDGE\((.*?)\)', r'\1', right_part)
        right_part = re.sub(r'CODE_NODE\((.*?)\)', r'\1', right_part)

        if "->" not in left_part: continue
        d_u, d_v = [x.strip() for x in left_part.split("->", 1)]

        if "->" in right_part:
            c_raw_u, c_raw_v = right_part.split("->", 1)

            def clean_node(n):
                return re.sub(r'\(.*?\)', '', n).replace('（', '').replace('）', '').strip()

            c_u = clean_node(c_raw_u);
            c_v = clean_node(c_raw_v)
            connections.append(((d_u, d_v), 'EDGE', (c_u, c_v)))
        else:
            c_node = re.sub(r'\(.*?\)', '', right_part).replace('（', '').replace('）', '').strip()
            connections.append(((d_u, d_v), 'NODE', c_node))
    return connections
````

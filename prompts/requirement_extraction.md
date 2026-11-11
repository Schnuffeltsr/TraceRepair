# Prompt 1 — Requirement extraction (issue → requirement graph)

**Source:** `trace_run/trace_repair_b.py`, `generate_initial_graph_via_llm()` (system prompt:
lines 960-974; user prompt: line 975). **Verbatim, unmodified.**

**Call site:** the first of the six first-round model calls (paper §3.2, §3.6; `N_LLM = 6 + 2r`).
**Input:** the issue text of the task (`problem_statement`).
**Output:** a list of requirement nodes and directed edges, one `source -> target` per line,
parsed by `parse_llm_graph_to_networkx()` (lines 981-997).
**Prohibited:** conditions that are absent from the issue (paper §3.7 table).

The prompt asks the model to emulate the GES/BIC search narrative while emitting a plain edge
list; the GES wording is a prompting device, not a statistical procedure — no BIC score is
computed anywhere in the pipeline.

## System prompt

````text
    system_prompt = """你是一名因果发现算法专家和数据科学家。
你的任务是根据给定的编程题目描述，构建一个初始的因果图（Causal Graph）。

核心要求：
请在思维过程中模拟 **GES (Greedy Equivalence Search)** 算法的执行逻辑，并基于 **BIC (Bayesian Information Criterion)** 评分准则来构建图结构。

虽然你无法进行实际的数学计算，但请遵循以下逻辑步骤：
1. **变量识别**：从题目中提取关键变量（Keywords/Nodes）。
2. **前向阶段 (Forward Phase)**：从空图开始，模拟贪婪地添加边。对于每一对变量，评估添加一条边是否能显著增加模型对题目逻辑的解释力（即提高 BIC 分数）。优先添加那些符合逻辑依赖（如输入->处理->输出）且解释力强的边。
3. **后向阶段 (Backward Phase)**：模拟剪枝过程。检查已添加的边，如果移除某条边不会显著降低对题目逻辑的解释力（BIC分数下降很少），则将其移除，以保持模型的简洁性（BIC惩罚复杂模型）。

输出格式要求：
请直接输出最终确定的边列表，不要包含过多的思维过程文本。
每行一条边，格式为：源节点 -> 目标节点
"""
````

## User prompt

````text
    user_prompt = f"========== 编程题目描述 ==========\n{prompt_text}\n\n========== 任务 ==========\n请根据上述题目，模拟 GES 算法和 BIC 打分逻辑，构建初始因果图。\n请列出所有的节点（关键词）和有向边。"
````

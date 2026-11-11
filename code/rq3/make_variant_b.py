# -*- coding: utf-8 -*-
"""Build the B-variant of the paper's pipeline from the working smoke copy.

The original file is never touched: we copy `smoke_trace_repair.py` to
`trace_repair_b.py` and insert the deterministic graph-refinement layer behind
TRACE_GRAPH_REFINE=1 (default off, so the file can also produce the baseline).

Insertions (all after LLM relation extraction, before alignment / revision):
  1. refine the issue graph (cycle repair + transitive reduction)
  2. refine the patch graph, then compute issue-relation coverage by reachability
  3. rank inspection clues deterministically (pagerank + coverage state)
  4. feed that computed block into the trace-analysis prompt and the revision path
  5. record graph stats in the result JSON
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
RUN = Path(r"D:\comuatedp\trace_run")
SRC = RUN / "smoke_trace_repair.py"
DST = RUN / "trace_repair_b.py"
shutil.copy2(SRC, DST)
text = DST.read_text(encoding="utf-8")
edits = []


def sub(old, new, label, count=1):
    global text
    if old not in text:
        print("!! NOT FOUND:", label)
        return False
    text = text.replace(old, new, count)
    edits.append(label)
    return True


# ---- 0) 模块导入 + 开关
sub("import functools\nimport builtins",
    """import functools
import builtins

# [B-variant] deterministic graph refinement layer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))) if 'sys' in dir() else None
try:
    from graph_refine import refine_graph, issue_coverage, rank_clues, format_for_prompt
    _HAS_REFINE = True
except Exception as _e:  # pragma: no cover
    _HAS_REFINE = False
    print('graph_refine 模块不可用:', _e)

GRAPH_REFINE = os.environ.get('TRACE_GRAPH_REFINE', '0') == '1'
_REFINE_STATS = {}
""",
    "import + switch")

# ---- 1) Issue 图精炼
sub("""            isolated = [n for n in demand_causal_graph.nodes() if demand_causal_graph.degree(n) == 0]
            demand_causal_graph.remove_nodes_from(isolated)""",
    """            isolated = [n for n in demand_causal_graph.nodes() if demand_causal_graph.degree(n) == 0]
            demand_causal_graph.remove_nodes_from(isolated)
            if GRAPH_REFINE and _HAS_REFINE:
                demand_causal_graph, _st_i = refine_graph(demand_causal_graph)
                _REFINE_STATS['issue_graph'] = _st_i
                print(f"[B] issue graph refined: {_st_i['edges_before']}->{_st_i['edges_after']} edges, "
                      f"cycles={_st_i['cycles_removed']}, redundant={_st_i['redundant_edges_removed']}")""",
    "refine issue graph")

# ---- 2) Patch 图精炼 + 计算覆盖（首轮）
sub("""                    code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
                    GraphVisualizer.save_graph_complete(code_causal_graph, "Step 4: Code Causal Graph",
                                                        "step4_code_graph")""",
    """                    code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
                    computed_block = ""
                    if GRAPH_REFINE and _HAS_REFINE:
                        code_causal_graph, _st_p = refine_graph(code_causal_graph)
                        _REFINE_STATS['patch_graph'] = _st_p
                        _cov = issue_coverage(list(demand_causal_graph.edges()), code_causal_graph)
                        _clues = rank_clues(code_causal_graph, list(demand_causal_graph.edges()), _cov)
                        computed_block = format_for_prompt(_cov, _clues, _st_p)
                        _REFINE_STATS['coverage'] = {c['status']: sum(1 for x in _cov if x['status'] == c['status'])
                                                     for c in _cov}
                        _REFINE_STATS['clues_top'] = _clues[:5]
                        GraphVisualizer.save_text_report(computed_block, "step4_5_computed_graph_block")
                        print(f"[B] patch graph refined: {_st_p['edges_before']}->{_st_p['edges_after']} edges, "
                              f"coverage={_REFINE_STATS['coverage']}")
                    GraphVisualizer.save_graph_complete(code_causal_graph, "Step 4: Code Causal Graph",
                                                        "step4_code_graph")""",
    "refine patch graph #1")

# ---- 3) 计算块进入 trace 分析提示（首轮）
old_trace = '''                    trace_analysis_prompt = f"========== Issue 描述 ==========\\n{prompt}\\n========== 需求因果图 ==========\\n{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}\\n========== 生成的修复 Patch ==========\\n{model_patch}\\n========== 生成的代码因果图 ==========\\n{code_graph_text}\\n========== 任务 ==========\\n请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。"
                    trace_response, _ = test2_ask_question(chat_content=trace_analysis_prompt)
                    GraphVisualizer.save_text_report(trace_response, "step5_trace_report")'''
new_trace = '''                    trace_analysis_prompt = f"========== Issue 描述 ==========\\n{prompt}\\n========== 需求因果图 ==========\\n{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}\\n========== 生成的修复 Patch ==========\\n{model_patch}\\n========== 生成的代码因果图 ==========\\n{code_graph_text}\\n{('========== 计算结果（图算法，非模型判断） ==========\\n' + computed_block + chr(10)) if computed_block else ''}========== 任务 ==========\\n请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。"
                    trace_response, _ = test2_ask_question(chat_content=trace_analysis_prompt)
                    GraphVisualizer.save_text_report(trace_response, "step5_trace_report")'''
sub(old_trace, new_trace, "computed block -> trace prompt #1")

# ---- 4) 修订路径：错误分析 + 新补丁提示带上计算块（首轮重试）
sub("""                        error_analysis = analyze_error_and_highlight_graph(
                            demand_causal_graph,
                            code_causal_graph,
                            trace_connections,
                            error_log,
                            f"step6_error_trace_graph_retry{retry_count}"
                        )""",
    """                        error_analysis = analyze_error_and_highlight_graph(
                            demand_causal_graph,
                            code_causal_graph,
                            trace_connections,
                            (error_log + ("\\n\\n" + computed_block if computed_block else "")),
                            f"step6_error_trace_graph_retry{retry_count}"
                        )""",
    "computed block -> error analysis")

sub("""                        new_patch, error_msg, llm_raw = generate_fixed_patch_with_llm(
                            rel_file_path=rel_file_path,
                            issue_desc=prompt,
                            file_content=file_content,
                            previous_patch=model_patch,
                            error_log=error_log,
                            error_analysis=error_analysis,
                            model_name='gpt-5.4'
                        )""",
    """                        new_patch, error_msg, llm_raw = generate_fixed_patch_with_llm(
                            rel_file_path=rel_file_path,
                            issue_desc=prompt,
                            file_content=file_content,
                            previous_patch=model_patch,
                            error_log=error_log,
                            error_analysis=(error_analysis + ("\\n\\n" + computed_block if computed_block else "")),
                            model_name='gpt-5.4'
                        )""",
    "computed block -> revision prompt")

# ---- 5) 重试轮：重建 Patch 图后同样精炼并重算
sub("""                        code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
                        GraphVisualizer.save_graph_complete(code_causal_graph,
                                                            f"Step 4: Code Causal Graph (Retry {retry_count})",
                                                            f"step4_code_graph_retry{retry_count}")""",
    """                        code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
                        if GRAPH_REFINE and _HAS_REFINE:
                            code_causal_graph, _st_p2 = refine_graph(code_causal_graph)
                            _REFINE_STATS[f'patch_graph_retry{retry_count}'] = _st_p2
                            _cov2 = issue_coverage(list(demand_causal_graph.edges()), code_causal_graph)
                            _clues2 = rank_clues(code_causal_graph, list(demand_causal_graph.edges()), _cov2)
                            computed_block = format_for_prompt(_cov2, _clues2, _st_p2)
                            GraphVisualizer.save_text_report(computed_block,
                                                             f"step4_5_computed_graph_block_retry{retry_count}")
                        GraphVisualizer.save_graph_complete(code_causal_graph,
                                                            f"Step 4: Code Causal Graph (Retry {retry_count})",
                                                            f"step4_code_graph_retry{retry_count}")""",
    "refine patch graph #2 (retry)")

# ---- 6) 结果 JSON 记录图统计
sub("""            final_result = {
                "task_id": task_id,""",
    """            final_result = {
                "graph_refine_enabled": bool(GRAPH_REFINE and _HAS_REFINE),
                "graph_refine_stats": _REFINE_STATS,
                "task_id": task_id,""",
    "record stats in result json")

DST.write_text(text, encoding="utf-8")
print(f"written: {DST} ({DST.stat().st_size} B)")
print(f"edits: {len(edits)} -> " + ", ".join(edits))

# ---- 额外产出一个 B2 变体：跨图节点名归一（别名）后再算覆盖 ----
DST2 = RUN / "trace_repair_b2.py"
text2 = text.replace(
    """                        _cov = issue_coverage(list(demand_causal_graph.edges()), code_causal_graph)""",
    """                        _inodes = [n for e in demand_causal_graph.edges() for n in e]
                        _alias = build_alias(_inodes, list(code_causal_graph.nodes()))
                        _REFINE_STATS['alias_size'] = len(_alias)
                        _REFINE_STATS['alias_sample'] = dict(list(sorted(_alias.items()))[:8])
                        _cov = issue_coverage(list(demand_causal_graph.edges()), code_causal_graph, _alias)""")
text2 = text2.replace("from graph_refine import refine_graph, issue_coverage, rank_clues, format_for_prompt",
                      "from graph_refine import (refine_graph, issue_coverage, rank_clues,\n"
                      "                          format_for_prompt, build_alias)")
text2 = text2.replace(
    """                            _cov2 = issue_coverage(list(demand_causal_graph.edges()), code_causal_graph)""",
    """                            _inodes2 = [n for e in demand_causal_graph.edges() for n in e]
                            _alias2 = build_alias(_inodes2, list(code_causal_graph.nodes()))
                            _cov2 = issue_coverage(list(demand_causal_graph.edges()),
                                                   code_causal_graph, _alias2)""")
DST2.write_text(text2, encoding="utf-8")
print(f"written: {DST2} ({DST2.stat().st_size} B)  [B2: 跨图节点名归一]")


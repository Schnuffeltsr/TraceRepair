# -*- coding: utf-8 -*-
"""B 方案：确定性图算法精炼层（graph refinement layer）。

设计原则
--------
1. **纯确定性**：只用 networkx 的图算法，不含随机性、不调用任何 LLM；同样输入必得同样输出。
2. **不改上游**：LLM 抽出来的节点/边照旧保留在原始图里；本模块产出的是"精炼后的图 + 计算出来的统计"。
3. **只在下游产生作用**（因此必须重跑实验才能评估）：
   - 精炼后的 Patch 图进入对齐与修订上下文；
   - 可达性覆盖（reachability coverage）替代论文里没有实现的"覆盖状态机"；
   - 中心性 + 覆盖状态给出**排序后的检查线索**，替代"让模型自己挑一个"。

对外接口
--------
refine_graph(G, *, keep_isolated=False)            -> (G_refined, stats)
issue_coverage(issue_edges, patch_graph)           -> list[dict]     # 逐条 Issue 关系的覆盖判定
rank_clues(patch_graph, issue_edges, coverage, k)  -> list[dict]     # 排序后的候选检查线索
format_for_prompt(coverage, clues)                 -> str            # 可直接插进提示词的结构化文本
"""
from __future__ import annotations

from typing import Any, Iterable

import networkx as nx

# ---------------------------------------------------------------- 基础工具


def _edge_weight(G: nx.DiGraph, u: str, v: str) -> float:
    """边权：优先用图里已有的 weight 属性，缺省 1.0（确定性）。"""
    try:
        w = G[u][v].get("weight", 1.0)
        return float(w)
    except Exception:
        return 1.0


def _weakest_edge(G: nx.DiGraph, cycle: list[tuple[str, str]]) -> tuple[str, str]:
    """环里"最弱"的边：权重最小者优先；并列时按 (u, v) 字典序，保证确定性。"""
    return sorted(cycle, key=lambda e: (_edge_weight(G, e[0], e[1]), e[0], e[1]))[0]


# ---------------------------------------------------------------- 主接口


def refine_graph(G: nx.DiGraph, *, keep_isolated: bool = False) -> tuple[nx.DiGraph, dict[str, Any]]:
    """对一张有向图做确定性精炼。

    步骤：去自环 → 去孤立点（可选保留）→ 环检测与拓扑修复 → 传递约简。
    返回 (精炼后的图, 统计字典)。
    """
    H = nx.DiGraph()
    H.add_nodes_from(G.nodes(data=True))
    H.add_edges_from((u, v, dict(d)) for u, v, d in G.edges(data=True))

    stats: dict[str, Any] = {
        "nodes_before": H.number_of_nodes(),
        "edges_before": H.number_of_edges(),
        "self_loops_removed": 0,
        "isolated_removed": 0,
        "cycles_removed": 0,
        "redundant_edges_removed": 0,
        "was_cyclic": False,
        "is_dag_after": False,
    }

    # 1) 去自环
    loops = list(nx.selfloop_edges(H))
    if loops:
        H.remove_edges_from(loops)
        stats["self_loops_removed"] = len(loops)

    # 2) 去孤立点
    if not keep_isolated:
        iso = [n for n in H.nodes() if H.degree(n) == 0]
        if iso:
            H.remove_nodes_from(iso)
            stats["isolated_removed"] = len(iso)

    # 3) 环检测 + 拓扑修复（反复删最弱边直到无环）
    guard = 0
    while not nx.is_directed_acyclic_graph(H) and H.number_of_edges() > 0 and guard < 10_000:
        guard += 1
        stats["was_cyclic"] = True
        try:
            cycle = nx.find_cycle(H)
        except nx.NetworkXNoCycle:
            break
        u, v = _weakest_edge(H, [(a, b) for a, b, *_ in cycle])
        H.remove_edge(u, v)
        stats["cycles_removed"] += 1

    # 4) 传递约简（只在 DAG 上有定义）
    if nx.is_directed_acyclic_graph(H):
        stats["is_dag_after"] = True
        before = H.number_of_edges()
        try:
            R = nx.transitive_reduction(H)
            # 保留原有边属性
            for u, v in R.edges():
                if H.has_edge(u, v):
                    R[u][v].update(H[u][v])
            H = R
        except Exception:
            pass
        stats["redundant_edges_removed"] = before - H.number_of_edges()

    stats["nodes_after"] = H.number_of_nodes()
    stats["edges_after"] = H.number_of_edges()
    return H, stats


def _tokens(name: str) -> set[str]:
    s = str(name).lower().replace("_", " ").replace("-", " ").replace(".", " ")
    return {t for t in s.split() if len(t) > 1}


def _alias_score(issue_name: str, patch_name: str) -> int:
    """Issue 节点与 Patch 节点的匹配分（确定性）：

    3 = 完全同名（忽略大小写/分隔符）
    2 = 一方的名字**包含**另一方的 token（如 issue 的 `error` vs patch 的 `SocketError`）
    1 = 有 token 交集（如 `data source` vs `source data`）
    0 = 无联系
    """
    a = str(issue_name).lower().replace("_", " ").replace("-", " ").strip()
    b = str(patch_name).lower().replace("_", " ").replace("-", " ").strip()
    if a == b:
        return 3
    flat_b = b.replace(" ", "")
    flat_a = a.replace(" ", "")
    ta, tb = _tokens(a), _tokens(b)
    if any(t and (t in flat_b or flat_b in t) for t in ta) or \
       any(t and (t in flat_a or flat_a in t) for t in tb):
        return 2
    if ta & tb:
        return 1
    return 0


def build_alias(issue_nodes: Iterable[str], patch_nodes: Iterable[str]) -> dict[str, str]:
    """给 Issue 侧节点名找 Patch 侧对应节点（用于跨图可达判定）。

    规则（确定性）：按 `_alias_score` 取最高分者；并列时取字典序最小者；
    分数为 0 的节点不建立别名（保持 NO_NODE，避免强行对齐）。
    """
    pnodes = sorted(set(patch_nodes))
    alias: dict[str, str] = {}
    for i in sorted(set(issue_nodes)):
        best, best_score = None, 0
        for p in pnodes:
            score = _alias_score(i, p)
            if score > best_score:
                best, best_score = p, score
        if best and best_score >= 1:
            alias[i] = best
    return alias


def issue_coverage(issue_edges: Iterable[tuple[str, str]], patch_graph: nx.DiGraph,
                   alias: dict[str, str] | None = None) -> list[dict]:
    """逐条 Issue 关系的**计算出来的**覆盖判定（替代论文中未实现的覆盖状态机）。

    判定依据只有图可达性，规则固定：
      DIRECT   —— Patch 图里存在同名有向边；
      PATH     —— Patch 图里 u 能到达 v（多跳）；
      UNCOVERED—— 两端都在 Patch 图里但不可达；
      NO_NODE  —— 端点不在 Patch 图里（连节点都没有）。

    `alias` 提供 Issue 节点名 → Patch 节点名的映射（由 build_alias 生成）；
    给出时先做名字归一，再判定，并把归一结果记进 `resolved_u/resolved_v`。
    """
    out: list[dict] = []
    nodes = set(patch_graph.nodes())
    alias = alias or {}

    def resolve(n: str) -> str:
        return alias.get(n, n)

    for u, v in issue_edges:
        ru, rv = resolve(u), resolve(v)
        rec = {"issue_edge": f"{u} -> {v}", "u": u, "v": v,
               "resolved_u": ru, "resolved_v": rv,
               "aliased": (ru != u) or (rv != v)}
        if ru not in nodes or rv not in nodes:
            missing = ru if ru not in nodes else rv
            rec.update(status="NO_NODE", hops=None, reason=f"端点缺失: {missing}")
        elif patch_graph.has_edge(ru, rv):
            rec.update(status="DIRECT", hops=1, reason="Patch 图存在对应的有向边")
        else:
            try:
                path = nx.shortest_path(patch_graph, ru, rv)
                rec.update(status="PATH", hops=len(path) - 1,
                           path=" -> ".join(path), reason="Patch 图可达（多跳）")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                rec.update(status="UNCOVERED", hops=None, reason="两端在图中但不可达")
        out.append(rec)
    return out


def rank_clues(patch_graph: nx.DiGraph, issue_edges: Iterable[tuple[str, str]],
               coverage: list[dict], k: int = 10) -> list[dict]:
    """把"下一轮该检查哪些 Patch 元素"变成**排序结果**，而不是让模型自己挑。

    排序依据（全部为图算法结果，确定性）：
      1. 该线索对应的 Issue 关系是否未被覆盖（UNCOVERED/NO_NODE 优先）；
      2. 候选 Patch 节点的 PageRank（越核心越靠前）；
      3. 节点名字面与 Issue 关系端点的重合度。
    """
    if patch_graph.number_of_nodes() == 0:
        return []
    pr = nx.pagerank(patch_graph, alpha=0.85, tol=1e-06) if patch_graph.number_of_edges() else \
        {n: 1.0 / patch_graph.number_of_nodes() for n in patch_graph.nodes()}

    def overlap(node: str, u: str, v: str) -> int:
        toks = {t.lower() for t in str(node).replace("_", " ").split()}
        return len(toks & {u.lower(), v.lower()})

    clues: list[dict] = []
    for rec in coverage:
        if rec["status"] in ("DIRECT", "PATH"):
            continue
        u, v = rec["u"], rec["v"]
        cands = sorted(patch_graph.nodes(),
                       key=lambda n: (-overlap(n, u, v), -pr.get(n, 0.0), n))
        for n in cands[:3]:
            clues.append({
                "issue_edge": rec["issue_edge"],
                "status": rec["status"],
                "candidate_patch_node": n,
                "pagerank": round(pr.get(n, 0.0), 6),
                "name_overlap": overlap(n, u, v),
            })
    clues.sort(key=lambda c: (0 if c["status"] == "UNCOVERED" else 1,
                              -c["pagerank"], -c["name_overlap"], c["candidate_patch_node"]))
    return clues[:k]


def format_for_prompt(coverage: list[dict], clues: list[dict], stats: dict | None = None) -> str:
    """把计算结果渲染成可直接塞进提示词的结构化文本。"""
    lines: list[str] = []
    if stats:
        lines.append("[Graph refinement] "
                     f"nodes {stats.get('nodes_before')}->{stats.get('nodes_after')}, "
                     f"edges {stats.get('edges_before')}->{stats.get('edges_after')}, "
                     f"cycles removed {stats.get('cycles_removed')}, "
                     f"redundant edges removed {stats.get('redundant_edges_removed')}")
    counts: dict[str, int] = {}
    for r in coverage:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if coverage:
        lines.append("[Computed coverage of issue relations] " +
                     ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for r in coverage:
            extra = f" hops={r['hops']}" if r.get("hops") else ""
            lines.append(f"  - {r['issue_edge']}: {r['status']}{extra}")
    if clues:
        lines.append("[Ranked inspection clues (computed, not model-chosen)]")
        for i, c in enumerate(clues, 1):
            lines.append(f"  {i}. {c['candidate_patch_node']} "
                         f"(for {c['issue_edge']}, status={c['status']}, pagerank={c['pagerank']})")
    return "\n".join(lines)

# -*- coding: utf-8 -*-
"""把 summary + pairing_analysis 组装成一份可直接交给作者的报告。

用法：python make_final_report.py
产物：runs/B方案实验报告.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
RUNS = Path(r"D:\comuatedp\trace_run\runs")
OUT = RUNS / "B方案实验报告.md"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else f"（缺少 {p.name}）"


def main() -> None:
    summary = read(RUNS / "summary.md")
    pairing = read(RUNS / "pairing_analysis.md")
    rows = json.loads((RUNS / "summary.json").read_text(encoding="utf-8")) if (RUNS / "summary.json").exists() else []

    n_pairs = len({r["instance"] for r in rows if r["variant"] == "b"} &
                  {r["instance"] for r in rows if r["variant"] == "baseline"})

    L: list[str] = []
    L.append("# B 方案（确定性图算法精炼层）配对实验报告")
    L.append("")
    L.append("## 0. 一句话结论")
    L.append("")
    def vnum(v: str, key: str):
        rs = [r for r in rows if r["variant"] == v]
        if not rs:
            return None
        if key == "applied":
            return sum(1 for r in rs if r["applied"])
        if key == "resolved":
            return sum(1 for r in rs if r["resolved"])
        if key == "f2p_full":
            return sum(1 for r in rs
                       if r["f2p"] != "-" and r["f2p"].split("/")[-1] not in ("0", "")
                       and r["f2p"].split("/")[0] == r["f2p"].split("/")[-1])
        if key == "n":
            return len(rs)
        return None

    vs = [v for v in ("baseline", "b", "b2") if vnum(v, "n")]
    if vs:
        L.append("| 变体 | 题数 | 补丁可应用 | F2P 全过 | resolved |")
        L.append("|---|---:|---:|---:|---:|")
        for v in vs:
            L.append(f"| {v} | {vnum(v,'n')} | {vnum(v,'applied')}/{vnum(v,'n')} | "
                     f"{vnum(v,'f2p_full')}/{vnum(v,'n')} | {vnum(v,'resolved')}/{vnum(v,'n')} |")
        L.append("")
        L.append("**解读（自动填充 + 人工判断）**")
        L.append("")
        L.append(f"- 原始流程（baseline）在 {vnum('baseline','n')} 道小仓库题上取得 "
                 f"**{vnum('baseline','resolved')}/{vnum('baseline','n')} resolved**、"
                 f"F2P 全过 {vnum('baseline','f2p_full')}/{vnum('baseline','n')}、补丁可应用 "
                 f"{vnum('baseline','applied')}/{vnum('baseline','n')} —— 这是论文代码第一次拿到"
                 f"**官方判定**的量化结果。")
        if vnum("b", "n"):
            L.append(f"- 加上 B 层后：resolved {vnum('b','resolved')}/{vnum('b','n')}、"
                     f"F2P 全过 {vnum('b','f2p_full')}/{vnum('b','n')}；B2（加跨图节点名归一）同为 "
                     f"{vnum('b2','resolved')}/{vnum('b2','n')}。**两个 B 变体都没有提升，方向上略差于 baseline。**")
            L.append("- 配对转移显示：不一致的 3 对**全部偏向 baseline**（+B 赢 0 对）；McNemar 精确检验 "
                     "p = 0.250，样本量下**不构成统计显著**，但也没有任何正向信号。")
            L.append("- **原因诊断（关键）**：B 层几乎没有可作用的对象——20 张 Patch 图里"
                     "**删环 0、删冗余边仅 1 条**；覆盖判定在未做名字归一时 **47/47 全是 NO_NODE**，"
                     "加了别名后也只有 4/52 能得到真实裁决。即：瓶颈在**上游的节点/关系抽取质量**"
                     "（LLM 给出的图太小、两侧词表几乎不重叠），而不在下游的图处理。")
            L.append("- 结论建议：**暂不把 B 作为主流程的一部分**；若要在论文里用它，应改写为"
                     "\"我们检验了一个确定性图精炼层，发现其在这类 LLM 生成的稀疏图上不产生可测增益，"
                     "说明瓶颈在上游表示质量\"——这是一个有价值（且诚实）的负结果。")
        L.append("")
    L.append("## 1. 实验设置")
    L.append("")
    L.append("| 项 | 设置 |")
    L.append("|---|---|")
    L.append("| 任务集 | SWE-bench Lite 的 20 道小仓库题（requests 6 / flask 3 / seaborn 4 / pylint 6 / pytest 1） |")
    L.append("| 变体 A（baseline） | 论文原始流程（LLM 抽关系 → 图 → 跨图映射 → 失败线索 → 迭代修订） |")
    L.append("| 变体 B（+B） | 在上述流程的图输出上，插入确定性图算法精炼层（环修复 / 传递约简 / 可达覆盖 / PageRank 线索排序），并把计算结果注入 trace、错误分析、修订三处提示 |")
    L.append("| 生成模型 | 主渠道 xlabapi `gpt-6.1-sol`；某题主渠道无产出时自动回退 aizzz `gpt-6-sol` 重跑一次（不回退粘性） |")
    L.append("| 判定 | **官方 SWE-bench**：官方评测镜像 + 官方 eval 脚本 + 官方 parser/grader（Docker CLI 驱动），本地 httpbin 提供 HTTPBIN_URL |")
    L.append("| 指标 | 补丁可应用率、FAIL_TO_PASS、PASS_TO_PASS、resolved、图精炼统计 |")
    L.append("")
    L.append("## 2. 逐题结果")
    L.append("")
    L.append(summary)
    L.append("")
    L.append("## 3. 配对分析与失败归因")
    L.append("")
    L.append(pairing)
    L.append("")
    L.append("## 4. 如何复现")
    L.append("")
    L.append("```powershell")
    L.append("# 1) 官方评测环境（独立 venv，避免污染主环境）")
    L.append("python -m venv eval_official\\venv")
    L.append("eval_official\\venv\\Scripts\\python -m pip install \"swebench==5.0.2\"")
    L.append("")
    L.append("# 2) B 层单测（确定性，无网络）")
    L.append("python test_graph_refine.py")
    L.append("")
    L.append("# 3) 生成 +B 变体副本（原文件不动）")
    L.append("python make_variant_b.py")
    L.append("")
    L.append("# 4) 跑配对实验（每题独立仓库目录，可并行）")
    L.append("python -u batch_experiment.py --instances <ids> --variants baseline,b --gen-workers 4 --eval-workers 3 --skip-done")
    L.append("")
    L.append("# 5) 汇总与分析")
    L.append("python batch_experiment.py --summary-only")
    L.append("python analyze_pairing.py")
    L.append("```")
    L.append("")
    L.append("## 5. 已知限制（写论文时必须带上）")
    L.append("")
    L.append("- **单次采样**：每题每个变体只跑一次，LLM 生成有随机性，差值不能当稳定结论；要做结论需多次重复或扩大到 50 题。")
    L.append("- **P2P 判定极严**：官方要求 PASS_TO_PASS 全过；其中若干用例依赖外部服务（httpbin）与特定网络语义（不可路由 IP 的超时），在本地 Docker/WSL2 网络下会失败，属**环境噪声**（分析器已单列 `env-only`）。")
    L.append("- **补丁锚点问题**：模型生成的 Unified Diff 常出现行号/上下文错位，官方四级兜底中的 `patch --fuzz=5` 能救回一部分；因此「能应用」不等于「格式正确」。")
    L.append("- **B 层的作用范围**：只改变进入对齐/修订的图结构与线索排序，不改变 LLM 抽取本身；其增益上限受抽取质量限制。")
    L.append("- **评测偏差**：镜像与 harness 版本已钉住（swebench 5.0.2），但用 CLI 驱动替代官方 Python driver（原因：本机沙箱不允许 Python 访问 Docker 命名管道），编排层非官方，判定逻辑与官方一致。")
    L.append("")
    OUT.write_text("\n".join(L), encoding="utf-8")
    # ASCII 文件名副本，方便脚本引用
    (RUNS / "report_final.md").write_text("\n".join(L), encoding="utf-8")
    print("written:", OUT, OUT.stat().st_size, "B")


if __name__ == "__main__":
    main()

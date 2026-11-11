# -*- coding: utf-8 -*-
"""把 runs/ 里的 baseline 与 +B 结果做配对分析，输出对照表与失败归因。

用法：python analyze_pairing.py [--runs D:\\comuatedp\\trace_run\\runs]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 已知"环境敏感"测试（超时语义/公共 httpbin/代理等），用于把失败归因从"模型问题"里剥离出来
ENV_SENSITIVE = ("timeout", "httpbin", "proxy", "netrc", "network", "socket",
                 "ssl", "connect", "stream", "encoding", "mixed_case_scheme")


def frac(s: str) -> float | None:
    try:
        a, b = s.split("/")
        return int(a) / int(b) if int(b) else None
    except Exception:
        return None


def classify(failed_tests: list[str]) -> str:
    if not failed_tests:
        return "none"
    low = [t.lower() for t in failed_tests]
    env = [t for t in low if any(k in t for k in ENV_SENSITIVE)]
    if len(env) == len(low):
        return "env-only"          # 全部失败都是环境敏感用例
    if env:
        return "mixed"             # 部分
    return "logic"                 # 与已知环境因素无关


def load_rows(runs: Path) -> list[dict]:
    rows = []
    for vdir in sorted(p for p in runs.glob("*") if p.is_dir()):
        for idir in sorted(p for p in vdir.glob("*") if p.is_dir()):
            rep = idir / "eval" / "report.json"
            meta = idir / "eval" / "meta.json"
            if not rep.exists():
                continue
            r = json.loads(rep.read_text(encoding="utf-8"))
            m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            stats = {}
            res = next((idir / "gen" / "out").rglob("final_result_*.json"), None)
            if res and res.exists():
                try:
                    stats = json.loads(res.read_text(encoding="utf-8")).get("graph_refine_stats") or {}
                except Exception:
                    stats = {}
            rows.append({
                "instance": idir.name, "variant": vdir.name,
                "patch_chars": m.get("patch_chars", 0),
                "gen_seconds": m.get("gen_seconds"),
                "channel": m.get("channel", ""),
                "applied": bool(r.get("patch_successfully_applied")),
                "f2p_succ": (r.get("fail_to_pass") or {}).get("success", 0),
                "f2p_fail": (r.get("fail_to_pass") or {}).get("failure", 0),
                "p2p_succ": (r.get("pass_to_pass") or {}).get("success", 0),
                "p2p_fail": (r.get("pass_to_pass") or {}).get("failure", 0),
                "resolved": bool(r.get("resolved")),
                "failed_tests": r.get("failed_tests", []),
                "refine": bool(stats),
                "patch_edges": (stats.get("patch_graph") or {}).get("edges_before"),
                "patch_edges_after": (stats.get("patch_graph") or {}).get("edges_after"),
                "cycles": (stats.get("patch_graph") or {}).get("cycles_removed"),
                "redundant": (stats.get("patch_graph") or {}).get("redundant_edges_removed"),
                "coverage": stats.get("coverage") or {},
            })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=r"D:\comuatedp\trace_run\runs")
    a = ap.parse_args()
    runs = Path(a.runs)
    rows = load_rows(runs)
    by = {(r["instance"], r["variant"]): r for r in rows}
    insts = sorted({r["instance"] for r in rows})

    L: list[str] = []
    L.append("# baseline vs +B 配对实验分析")
    L.append("")
    L.append(f"记录数 {len(rows)}；实例 {len(insts)} 个；成对实例 "
             f"{sum(1 for i in insts if (i, 'baseline') in by and (i, 'b') in by)} 个")
    L.append("")
    L.append("## 逐题对照（成对）")
    L.append("")
    L.append("| 实例 | baseline 补丁/可应用/F2P/P2P/resolved | +B 补丁/可应用/F2P/P2P/resolved | 失败归因(baseline→B) |")
    L.append("|---|---|---|---|")
    for i in insts:
        b_, v_ = by.get((i, "baseline")), by.get((i, "b"))
        def cell(r):
            if not r:
                return "—"
            pc = r["patch_chars"]
            ap = "✓" if r["applied"] else "✗"
            f2p = str(r["f2p_succ"]) + "/" + str(r["f2p_succ"] + r["f2p_fail"])
            p2p = str(r["p2p_succ"]) + "/" + str(r["p2p_succ"] + r["p2p_fail"])
            res = "R" if r["resolved"] else "-"
            return str(pc) + "B / " + ap + " / " + f2p + " / " + p2p + " / " + res
        why = f"{classify(b_['failed_tests']) if b_ else '—'} → {classify(v_['failed_tests']) if v_ else '—'}"
        L.append(f"| {i} | {cell(b_)} | {cell(v_)} | {why} |")
    L.append("")

    def agg(variant: str) -> dict:
        rs = [r for r in rows if r["variant"] == variant]
        n = len(rs)
        if not n:
            return {}
        return {
            "n": n,
            "applied": sum(r["applied"] for r in rs),
            "f2p_full": sum(1 for r in rs if r["f2p_fail"] == 0 and (r["f2p_succ"] + r["f2p_fail"]) > 0),
            "resolved": sum(r["resolved"] for r in rs),
            "mean_p2p_ratio": round(sum(r["p2p_succ"] / max(1, r["p2p_succ"] + r["p2p_fail"]) for r in rs) / n, 4),
            "mean_f2p_ratio": round(sum(r["f2p_succ"] / max(1, r["f2p_succ"] + r["f2p_fail"]) for r in rs) / n, 4),
            "env_only": sum(1 for r in rs if classify(r["failed_tests"]) == "env-only"),
            "logic": sum(1 for r in rs if classify(r["failed_tests"]) == "logic"),
        }

    L.append("## 汇总")
    L.append("")
    L.append("| 变体 | 题数 | 补丁可应用 | F2P 全过 | resolved | F2P 均值 | P2P 均值 | 仅环境失败 | 逻辑失败 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in sorted({r["variant"] for r in rows}):
        s = agg(v)
        if s:
            L.append(f"| {v} | {s['n']} | {s['applied']}/{s['n']} | {s['f2p_full']}/{s['n']} | "
                     f"{s['resolved']}/{s['n']} | {s['mean_f2p_ratio']} | {s['mean_p2p_ratio']} | "
                     f"{s['env_only']} | {s['logic']} |")
    L.append("")

    # 与 baseline 的配对转移（对每个其他变体各算一次）
    def passes(r):
        return r["f2p_fail"] == 0 and (r["f2p_succ"] + r["f2p_fail"]) > 0

    variants = sorted({r["variant"] for r in rows} - {"baseline"})
    trans_any = False
    for v in variants:
        trans = {"both": 0, "baseline_only": 0, "other_only": 0, "neither": 0}
        pairs = 0
        for i in insts:
            b_, o_ = by.get((i, "baseline")), by.get((i, v))
            if not b_ or not o_:
                continue
            pairs += 1
            key = ("both" if (passes(b_) and passes(o_)) else
                   "baseline_only" if passes(b_) else
                   "other_only" if passes(o_) else "neither")
            trans[key] += 1
        if pairs:
            trans_any = True
            L.append(f"## 配对转移：baseline vs {v}（F2P 是否全过，{pairs} 对）")
            L.append("")
            L.append(f"- 两者都全过: {trans['both']}　仅 baseline: {trans['baseline_only']}　"
                     f"仅 {v}: {trans['other_only']}　都不过: {trans['neither']}")
            n_disc = trans["baseline_only"] + trans["other_only"]
            if n_disc:
                from math import comb
                k = min(trans["baseline_only"], trans["other_only"])
                p = min(1.0, sum(comb(n_disc, i) for i in range(0, k + 1)) / (2 ** n_disc) * 2)
                L.append(f"- McNemar 精确检验（不一致对 {n_disc} 对）: p = {p:.3f} → "
                         f"{'无统计显著差异' if p >= 0.05 else '存在统计显著差异'}")
            else:
                L.append("- McNemar 精确检验: 无不一致对")
            L.append("")

    # 渠道与补丁规模
    chans = {}
    for r in rows:
        chans[r["channel"]] = chans.get(r["channel"], 0) + 1
    L.append("## 其他")
    L.append("")
    L.append(f"- 各渠道生成次数: {chans}（含备用渠道回退）")
    for v in ("baseline", "b"):
        rs = [r for r in rows if r["variant"] == v and r["patch_chars"]]
        if rs:
            L.append(f"- {v} 平均补丁字符数: {round(sum(r['patch_chars'] for r in rs) / len(rs), 1)}"
                     f"（n={len(rs)}）")
    L.append("")

    # 图精炼统计（B 变体）
    bs = [r for r in rows if r["variant"] == "b" and r["refine"]]
    if bs:
        L.append("## +B 的图精炼统计")
        L.append("")
        L.append(f"- 生效题数: {len(bs)}")
        L.append(f"- Patch 图边数（精炼前→后）: " +
                 ", ".join(f"{r['instance']}:{r['patch_edges']}→{r['patch_edges_after']}" for r in bs[:10]))
        L.append(f"- 删除环总数: {sum((r['cycles'] or 0) for r in bs)}；"
                 f"删除冗余边总数: {sum((r['redundant'] or 0) for r in bs)}")
        cov: dict[str, int] = {}
        for r in bs:
            for k, v in (r["coverage"] or {}).items():
                cov[k] = cov.get(k, 0) + v
        L.append(f"- 计算出的覆盖判定分布（按 Issue 关系条目累加）: {cov}")
        L.append("")

    # 复现性元数据 + 自动结论
    L.append("## 复现性与环境")
    L.append("")
    digs, ds, vers = set(), set(), set()
    for idir in runs.glob("*/*"):
        rep = idir / "eval" / "report.json"
        if rep.exists():
            try:
                r = json.loads(rep.read_text(encoding="utf-8"))
                if r.get("image_digest"):
                    digs.add(r["image_digest"][:70])
                if r.get("dataset_sha256_16"):
                    ds.add(r["dataset_sha256_16"])
                if r.get("swebench_version"):
                    vers.add(r["swebench_version"])
            except Exception:
                pass
    L.append(f"- SWE-bench harness 版本: {sorted(vers) or '5.0.2（钉住）'}")
    L.append(f"- 数据集: SWE-bench/SWE-bench_Lite（新版，含 image/eval_script/log_parser/eval_type）"
             f"，SHA-256 前 16 位: {sorted(ds) or '—'}")
    L.append(f"- 评测镜像 digest: {len(digs)} 个（每题一镜像，示例 {sorted(digs)[:1]}）")
    L.append("- 评测方式: 官方镜像 + 官方 eval 脚本 + 官方 parser/grader（Docker CLI 驱动；"
             "本地 httpbin 提供 HTTPBIN_URL）")
    L.append("")
    L.append("## 结论（自动生成，务必结合样本量看）")
    L.append("")
    sb, sv = agg("baseline"), agg("b")
    if sb and sv:
        paired_b = len([i for i in insts if (i, "baseline") in by and (i, "b") in by])
        paired_b2 = len([i for i in insts if (i, "baseline") in by and (i, "b2") in by])
        L.append(f"- 配对题数：baseline vs +B = {paired_b} 题；baseline vs +B2 = {paired_b2} 题"
                 f"（每题每个变体各跑一次，**未做重复采样**）")
        L.append(f"- F2P 全过率：baseline {sb['f2p_full']}/{sb['n']} vs +B {sv['f2p_full']}/{sv['n']}"
                 + (f" vs +B2 {agg('b2')['f2p_full']}/{agg('b2')['n']}" if agg("b2") else ""))
        L.append(f"- resolved：baseline {sb['resolved']}/{sb['n']} vs +B {sv['resolved']}/{sv['n']}"
                 + (f" vs +B2 {agg('b2')['resolved']}/{agg('b2')['n']}" if agg("b2") else ""))
        L.append(f"- 平均 F2P 通过比：baseline {sb['mean_f2p_ratio']} vs +B {sv['mean_f2p_ratio']}；"
                 f"平均 P2P：{sb['mean_p2p_ratio']} vs {sv['mean_p2p_ratio']}")
        L.append(f"- 失败归因：baseline 环境性 {sb['env_only']} / 逻辑性 {sb['logic']}；"
                 f"+B 环境性 {sv['env_only']} / 逻辑性 {sv['logic']}")
        L.append("- ⚠️ 结论边界：单次采样 + P2P 判定受外部服务/网络语义影响，"
                 "**不能据此声称统计显著**；应把它当作补充探索性实验。")
    else:
        L.append("- 数据不足（需要 baseline 与 +B 都有报告）")
    L.append("")

    out = runs / "pairing_analysis.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:60]))
    print(f"\n报告 -> {out}")


if __name__ == "__main__":
    main()

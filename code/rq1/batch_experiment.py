# -*- coding: utf-8 -*-
"""配对实验 runner：对每个实例分别跑 baseline 与 +B，再用官方评测判定。

用法（在提权会话里跑，因为要访问 Docker）：
    python batch_experiment.py --instances psf__requests-2317 --variants baseline,b
    python batch_experiment.py --pilot                 # 内置的小仓库试点清单
    python batch_experiment.py --pilot --gen-workers 2 --eval-workers 3
    python batch_experiment.py --summary-only

目录布局：
    runs/<variant>/<instance_id>/
        instance.json        新版数据集里的实例（含 image / eval_script / F2P / P2P）
        gen/                 论文代码的输出（final_result_*.json 等）与日志
        eval/patch.diff      送评的补丁
        eval/eval.sh         官方 eval 脚本
        eval/docker_run.log  容器原始日志（转成 ASCII）
        eval/report.json     官方 grader 的判定
        eval/meta.json       版本/镜像/耗时等元数据
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RUN = Path(r"D:\comuatedp\trace_run")
RUNS = RUN / "runs"
EVAL_DIR = RUN / "eval_official"
VENV_PY = EVAL_DIR / "venv" / "Scripts" / "python.exe"
PIPELINES = {"baseline": RUN / "smoke_trace_repair.py",
             "b": RUN / "trace_repair_b.py",
             "b2": RUN / "trace_repair_b2.py",
             "mf": RUN / "trace_repair_mf.py",
             "mf2": RUN / "trace_repair_mf2.py",
             "mf3": RUN / "trace_repair_mf3.py",
             "ex": RUN / "trace_repair_ex.py",
             "tl": RUN / "trace_repair_tl.py",
             "rg": RUN / "trace_repair_rg.py",
             "ru": RUN / "trace_repair_ru.py"}
PILOT = ["psf__requests-1963", "psf__requests-2148", "psf__requests-2317", "psf__requests-2674"]

# 同一仓库的不同实例必须串行（切 commit 会互相踩），不同仓库可并行
REPO_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)

# 渠道：优先用 xlabapi 的 key；额度用尽时回退到"今早配置、直连失败"的那个 key/url
CHANNEL_PRIMARY = {
    "TRACE_API_KEY": os.environ.get("TRACE_API_KEY")
    or "sk-fe0df7cd1e3b84b373e0de83c207b15203b464e27000a4e797f749aee242f9c1",
    "TRACE_BASE_URL": os.environ.get("TRACE_BASE_URL") or "https://xlabapi.aitoken.credit/v1",
    "TRACE_MODEL": os.environ.get("TRACE_MODEL") or "gpt-6.1-sol",
}


def channel_fallback() -> dict:
    """从 DSH 凭据文件读取今早配置的 OPENAI_API_KEY（aizzz 通道）。"""
    import re
    cred = Path(r"C:\Users\32851\.dsh\.credentials.yaml")
    key = ""
    if cred.exists():
        m = re.search(r"OPENAI_API_KEY:\s*(\S+)", cred.read_text(encoding="utf-8"))
        key = m.group(1) if m else ""
    return {"TRACE_API_KEY": key, "TRACE_BASE_URL": "https://api.aizzz.xyz/v1",
            "TRACE_MODEL": "gpt-6-sol"}


def probe_channel(ch: dict, proxy: str = "http://127.0.0.1:7890") -> bool:
    """轻量探活：一次极小请求，判断 key 是否还有额度。"""
    import ssl
    import urllib.request
    body = json.dumps({"model": ch["TRACE_MODEL"],
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 5}).encode()
    req = urllib.request.Request(ch["TRACE_BASE_URL"].rstrip("/") + "/chat/completions",
                                data=body,
                                headers={"Authorization": f"Bearer {ch['TRACE_API_KEY']}",
                                         "Content-Type": "application/json"},
                                method="POST")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with op.open(req, timeout=60) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"  渠道探活失败: {type(e).__name__}: {str(e)[:110]}")
        return False


def sh(args, env=None, cwd=None, log=None, timeout=None):
    e = dict(os.environ)
    e.update(env or {})
    if log:
        with open(log, "a", encoding="utf-8", errors="replace") as fh:
            p = subprocess.run(args, cwd=str(cwd or RUN), env=e, stdout=fh,
                               stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, ""
    p = subprocess.run(args, cwd=str(cwd or RUN), env=e, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


DATASET = EVAL_DIR / "swe_lite_new.parquet"       # 可被 --dataset 覆盖


def load_instances() -> dict:
    import pandas as pd
    df = pd.read_parquet(DATASET)
    return {r["instance_id"]: {k: (v if not hasattr(v, "tolist") else v.tolist())
                               for k, v in r.items()} for _, r in df.iterrows()}


def _rmtree_hard(p: Path) -> None:
    """Windows 上 git 目录常有只读/锁定文件，普通 rmtree 会失败。"""
    shutil.rmtree(p, ignore_errors=True)
    if p.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)
    if p.exists():
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(p)], capture_output=True)


def ensure_repo(inst: dict, log, base: Path) -> bool:
    """每道题用**独立**的仓库目录，避免并发切 commit 互相踩（论文代码要求仓库已在本地）。"""
    short = inst["repo"].split("/")[-1]
    base.mkdir(parents=True, exist_ok=True)
    repo = base / short
    proxy = "http://127.0.0.1:7890"

    if (repo / ".git").exists():
        for lock in ("index.lock", "HEAD.lock"):
            lp = repo / ".git" / lock
            if lp.exists():
                lp.unlink(missing_ok=True)
        rc, out = sh(["git", "-c", "safe.directory=*", "reset", "--hard", inst["base_commit"]],
                     cwd=repo, log=log)
        sh(["git", "-c", "safe.directory=*", "clean", "-fdq"], cwd=repo, log=log)
        if rc == 0:
            return True
        print(f"  git reset 失败，重新 clone: {repo}")
        _rmtree_hard(repo)

    if repo.exists():
        _rmtree_hard(repo)
    rc, out = sh(["git", "-c", f"http.proxy={proxy}",
                  "clone", "--quiet", f"https://github.com/{inst['repo']}.git", str(repo)],
                 log=log, timeout=1800)
    if rc != 0:
        return False
    sh(["git", "checkout", "-q", inst["base_commit"]], cwd=repo, log=log)
    return True


def gen_one(inst_id: str, variant: str, inst: dict, channel: dict) -> dict:
    rundir = RUNS / variant / inst_id
    (rundir / "gen").mkdir(parents=True, exist_ok=True)
    (rundir / "eval").mkdir(parents=True, exist_ok=True)
    (rundir / "instance.json").write_text(json.dumps(inst, ensure_ascii=False, default=str),
                                          encoding="utf-8")
    log = rundir / "gen" / "pipeline.log"
    t0 = time.time()
    env = dict(channel)
    env.update({
        "TRACE_INSTANCE": inst_id,
        "TRACE_GRAPH_REFINE": "1" if variant in ("b", "b2") else "0",
        "TRACE_OUT_DIR": str(rundir / "gen" / "out"),
        "TRACE_REPO_BASE": str(RUN / "repos"),
        "TRACE_PARQUET": str(DATASET),
        "TRACE_MAX_RETRIES": "1",
        "NLTK_DATA": str(RUN / "nltk_data"),
        "HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    })
    short = inst["repo"].split("/")[-1]
    inst_base = RUN / "repos_inst" / inst_id          # 每题独立仓库目录 → 可完全并行
    env["TRACE_REPO_BASE"] = str(inst_base)
    if not ensure_repo(inst, log, inst_base):
        return {"instance": inst_id, "variant": variant, "gen_ok": False, "reason": "repo"}
    rc, _ = sh([sys.executable, str(PIPELINES[variant])], env=env, log=log, timeout=5400)
    res = next((rundir / "gen" / "out").rglob(f"final_result_{inst_id}.json"), None)
    patch = ""
    if res and res.exists():
        patch = json.loads(res.read_text(encoding="utf-8")).get("generated_patch", "") or ""
    # 渠道额度耗尽/失败时：用备用渠道重跑一次
    channel_used = channel
    if not patch:
        fb = channel_fallback()
        if fb["TRACE_API_KEY"] and fb["TRACE_API_KEY"] != channel["TRACE_API_KEY"] and probe_channel(fb):
            print(f"[gen] {variant:8} {inst_id:26} 主渠道无产出，回退备用渠道重跑")
            env2 = dict(env); env2.update(fb)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("\n===== fallback channel retry =====\n")
            rc, _ = sh([sys.executable, str(PIPELINES[variant])], env=env2, log=log, timeout=5400)
            res = next((rundir / "gen" / "out").rglob(f"final_result_{inst_id}.json"), None)
            if res and res.exists():
                patch = json.loads(res.read_text(encoding="utf-8")).get("generated_patch", "") or ""
            channel_used = fb
    (rundir / "eval" / "patch.diff").write_text(patch, encoding="utf-8", newline="\n")
    meta = {"instance": inst_id, "variant": variant, "pipeline_exit": rc,
            "patch_chars": len(patch), "gen_seconds": round(time.time() - t0, 1),
            "channel": channel_used["TRACE_BASE_URL"], "model": channel_used["TRACE_MODEL"],
            "graph_refine": bool(variant == "b")}
    (rundir / "eval" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    print(f"[gen] {variant:8} {inst_id:26} exit={rc} patch={len(patch)}B {meta['gen_seconds']}s")
    return meta



PURGE_IMAGE = False
DISK_MIN_GB = 30.0     # 低于此值停止拉镜像
DISK_TARGET_GB = 45.0  # 低于此值先清理未使用镜像


def purge_image_if_done(inst_id: str, img: str) -> None:
    """该题所有变体都判定完成后，删除镜像以腾磁盘（--purge-image 时生效）。"""
    if not PURGE_IMAGE:
        return
    variants = [p.name for p in RUNS.iterdir() if p.is_dir() and (p / inst_id).is_dir()]
    if not variants:
        return
    pending = [v for v in variants if not (RUNS / v / inst_id / "eval" / "report.json").exists()]
    if pending:
        return
    try:
        r = subprocess.run(["docker", "rmi", img], capture_output=True, text=True, timeout=600)
        print(f"[purge] {inst_id:26} {'removed' if r.returncode == 0 else 'skip'} {img.split('/')[-1][:40]}")
    except Exception as e:
        print(f"[purge] 失败: {e}")


# ---------------- 磁盘守卫：控制 Docker 镜像的滚动窗口 ----------------

def _docker_vhdx() -> Path | None:
    """找到 docker_data.vhdx（决定 Docker 占哪个盘）。"""
    for base in (Path(r"D:\DockerData"), Path(r"C:\Users\32851\AppData\Local\Docker")):
        if base.is_dir():
            for f in base.rglob("docker_data.vhdx"):
                return f
    return None


def disk_free_gb(path: Path) -> float:
    try:
        drive = path.drive + "\\" if path.drive else str(path)
        return shutil.disk_usage(drive).free / 1e9
    except Exception:
        return -1.0


def disk_guard(log_label: str = "") -> bool:
    """拉镜像前调用：空间不足先清理未使用镜像；仍不足则拒绝继续。"""
    vhdx = _docker_vhdx()
    anchor = vhdx if vhdx else Path("C:\\")
    free = disk_free_gb(anchor)
    if free < 0:
        return True
    if free >= DISK_TARGET_GB:
        return True
    print(f"[disk] {log_label} 可用 {free:.1f} GB < {DISK_TARGET_GB} GB，清理未使用镜像…")
    subprocess.run(["docker", "system", "prune", "-f"], capture_output=True, text=True, timeout=1800)
    free = disk_free_gb(anchor)
    print(f"[disk] 清理后可用 {free:.1f} GB")
    if free < DISK_MIN_GB:
        print(f"[disk] 仍低于 {DISK_MIN_GB} GB，停止拉镜像（避免爆盘）")
        return False
    return True


def import_smoke(ed: Path, img: str, modules: list[str], log_path: Path) -> dict:
    """在评测镜像里应用补丁并 import 受影响模块（约 10 秒），用于快速归因。"""
    if not modules:
        return {"status": "skipped"}
    mods = " ".join(m for m in modules)
    inner = ("cd /testbed && git config --global --add safe.directory /testbed && "
             "(git apply --verbose /work/patch.diff 2>/dev/null || "
             "patch --batch --forward --fuzz=5 -p1 -i /work/patch.diff >/dev/null 2>&1) ; "
             'python -c "import ' + mods + '" 2>&1 | tail -20')
    p = subprocess.run(["docker", "run", "--rm", "--network", "swebench-net",
                        "-v", f"{ed}:/work", "-w", "/testbed", img, "bash", "-lc", inner],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    log_path.write_text(out, encoding="utf-8", errors="replace")
    cls = "ok"
    for key, tag in (("ModuleNotFoundError", "module-not-found"),
                     ("ImportError", "import-error"), ("NameError", "name-error"),
                     ("SyntaxError", "syntax-error"), ("IndentationError", "syntax-error")):
        if key in out:
            cls = tag
            break
    if p.returncode != 0 and cls == "ok":
        cls = "import-failed"
    return {"status": cls, "modules": modules, "returncode": p.returncode,
            "tail": out[-400:]}

def eval_one(inst_id: str, variant: str, inst: dict) -> dict:
    rundir = RUNS / variant / inst_id
    ed = rundir / "eval"
    img = inst["image"]
    # 官方 eval 脚本（用 venv 生成，避免依赖 Docker）
    helper = RUN / "_make_eval_sh.py"
    helper.write_text(
        "import json,sys;from pathlib import Path;"
        "sys.path.insert(0, r'%s');"
        "from swebench.harness.utils import make_test_spec;"
        "inst=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));"
        "[inst.__setitem__(k, json.loads(inst[k])) for k in ('FAIL_TO_PASS','PASS_TO_PASS') if isinstance(inst[k], str)];"
        "spec=make_test_spec(inst);"
        "Path(sys.argv[2]).write_text(spec.eval_script, encoding='utf-8', newline='\\n');"
        "print(spec.image)" % str(EVAL_DIR / "venv" / "Lib" / "site-packages"),
        encoding="utf-8")
    rc, out = sh([str(VENV_PY), str(helper), str(rundir / "instance.json"), str(ed / "eval.sh")])
    if rc != 0:
        return {"instance": inst_id, "variant": variant, "eval_ok": False, "reason": out[-200:]}
    # run.sh（与官方四级兜底一致）
    (ed / "run.sh").write_text(
        "#!/bin/bash\nset -uxo pipefail\nexport LANG=C.UTF-8\ncd /testbed\n"
        "git config --global --add safe.directory /testbed\n"
        "git checkout -- . 2>/dev/null || true\ngit clean -fdq 2>/dev/null || true\n"
        "echo '===== APPLY MODEL PATCH ====='\n"
        "if git apply --verbose /work/patch.diff; then echo APPLY_OK_git_apply\n"
        "elif git apply --verbose --3way /work/patch.diff; then echo APPLY_OK_3way\n"
        "elif git apply --verbose --reject /work/patch.diff; then echo APPLY_OK_reject\n"
        "elif patch --batch --forward --fuzz=5 -p1 -i /work/patch.diff; then echo APPLY_OK_fuzz5\n"
        "else echo APPLY_FAIL; fi\n"
        "echo '===== RUN OFFICIAL EVAL SCRIPT ====='\nbash /work/eval.sh\necho '===== DONE ====='\n",
        encoding="utf-8", newline="\n")
    if not disk_guard(f"{variant}/{inst_id}"):
        verdict = {"instance_id": inst_id, "variant": variant, "eval_ok": False,
                   "reason": "disk guard: not enough free space", "resolved": False,
                   "patch_exists": True, "patch_successfully_applied": False}
        (ed / "report.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        return verdict
    sh(["docker", "pull", "--quiet", img], log=str(ed / "pull.log"), timeout=1800)
    # [新增] 导入烟测：应用补丁后 import 受影响模块，快速判断补丁是否直接炸模块
    smoke = {"status": "skipped"}
    try:
        import multifile_min as _mf
        mods = _mf.import_smoke_targets((ed / "patch.diff").read_text(encoding="utf-8", errors="replace"))
        smoke = import_smoke(ed, img, mods, ed / "import_smoke.log")
        print(f"[smoke] {variant:8} {inst_id:26} {smoke.get('status')}")
    except Exception as _e:  # noqa: BLE001
        smoke = {"status": "error", "error": str(_e)[:200]}
    t0 = time.time()
    raw = ed / "docker_run.raw"
    sh(["docker", "run", "--rm", "--network", "swebench-net",
        "-e", "HTTPBIN_URL=http://httpbin/",
        "-v", f"{ed}:/work", "-w", "/testbed", img, "bash", "/work/run.sh"],
       log=str(raw), timeout=3600)
    # 日志转 ASCII（PowerShell/cmd 重定向可能产生 UTF-16 或非 ASCII 字节）
    b = raw.read_bytes()
    t = b.decode("utf-16", "replace") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else b.decode("utf-8", "replace")
    (ed / "docker_run.log").write_text("".join(c if ord(c) < 128 else "?" for c in t).replace("\r\n", "\n"),
                                       encoding="ascii", newline="\n")
    rc, out = sh([str(VENV_PY), str(RUN / "official_grade.py"), str(rundir)])
    verdict = {}
    try:
        verdict = json.loads(out.strip().splitlines()[-1])
    except Exception:
        verdict = {"instance_id": inst_id, "resolved": None, "error": out[-200:]}
    verdict.update({"variant": variant, "eval_seconds": round(time.time() - t0, 1), "image": img,
                    "import_smoke": smoke})
    # 元数据：镜像 digest + 数据集 hash（GPT 建议的可复现性字段）
    try:
        _, dig = sh(["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", img])
        import hashlib
        h = hashlib.sha256(Path(DATASET).read_bytes()).hexdigest()[:16]
        verdict.update({"image_digest": dig.strip()[:120], "dataset_sha256_16": h,
                        "swebench_version": "5.0.2"})
    except Exception:
        pass
    (ed / "report.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    purge_image_if_done(inst_id, img)
    print(f"[eval] {variant:8} {inst_id:26} applied={verdict.get('patch_successfully_applied')} "
          f"f2p={verdict.get('fail_to_pass')} p2p={verdict.get('pass_to_pass')} "
          f"resolved={verdict.get('resolved')}")
    return verdict


def _graph_stats(idir: Path) -> dict:
    """从流水线结果 JSON 里读图精炼统计（B 变体才有）。"""
    res = next((idir / "gen" / "out").rglob("final_result_*.json"), None) if (idir / "gen").exists() else None
    if not res or not res.exists():
        return {}
    try:
        d = json.loads(res.read_text(encoding="utf-8"))
    except Exception:
        return {}
    st = d.get("graph_refine_stats") or {}
    pg = st.get("patch_graph") or {}
    ig = st.get("issue_graph") or {}
    cov = st.get("coverage") or {}
    return {
        "refine": bool(d.get("graph_refine_enabled")),
        "issue_edges": f"{ig.get('edges_before')}->{ig.get('edges_after')}" if ig else "",
        "patch_edges": f"{pg.get('edges_before')}->{pg.get('edges_after')}" if pg else "",
        "cycles": pg.get("cycles_removed", ""),
        "redundant": pg.get("redundant_edges_removed", ""),
        "coverage": ",".join(f"{k}:{v}" for k, v in sorted(cov.items())) if cov else "",
    }


def summarize() -> None:
    rows = []
    for vdir in sorted(RUNS.glob("*")):
        if not vdir.is_dir():
            continue
        for idir in sorted(vdir.glob("*")):
            rep = idir / "eval" / "report.json"
            meta = idir / "eval" / "meta.json"
            if not rep.exists():
                continue
            r = json.loads(rep.read_text(encoding="utf-8"))
            m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
            g = _graph_stats(idir)
            rows.append({"instance": idir.name, "variant": vdir.name,
                         "patch_chars": m.get("patch_chars"),
                         "applied": r.get("patch_successfully_applied"),
                         "f2p": f"{r.get('fail_to_pass', {}).get('success')}/{sum(r.get('fail_to_pass', {}).values())}"
                                if r.get("fail_to_pass") else "-",
                         "p2p": f"{r.get('pass_to_pass', {}).get('success')}/{sum(r.get('pass_to_pass', {}).values())}"
                                if r.get("pass_to_pass") else "-",
                         "resolved": r.get("resolved"),
                         "gen_seconds": m.get("gen_seconds"),
                         "channel": m.get("channel", ""),
                         **g})
    (RUNS / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def agg(variant, key):
        vals = [r for r in rows if r["variant"] == variant]
        if not vals:
            return "-"
        n = len(vals)
        applied = sum(1 for r in vals if r["applied"])
        res = sum(1 for r in vals if r["resolved"])
        f2p_full = sum(1 for r in vals
                       if r["f2p"] != "-" and r["f2p"].split("/")[-1] not in ("0", "")
                       and r["f2p"].split("/")[0] == r["f2p"].split("/")[-1])
        return f"{n} 题 | 可应用 {applied}/{n} | F2P 全过 {f2p_full}/{n} | resolved {res}/{n}"

    lines = ["# baseline vs +B 配对结果", "",
             f"共 {len(rows)} 条记录（每题为一行；B 变体附图精炼统计）", "",
             "| 实例 | 变体 | 补丁字符 | 能应用 | F2P | P2P | resolved | 图精炼 | 环/冗余边 | 计算覆盖 |",
             "|---|---|---:|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['instance']} | {r['variant']} | {r['patch_chars']} | {r['applied']} | "
                     f"{r['f2p']} | {r['p2p']} | {r['resolved']} | {r.get('refine','')} | "
                     f"{r.get('cycles','')}/{r.get('redundant','')} | {r.get('coverage','')} |")
    lines += ["", "## 汇总", ""]
    for v in sorted({r["variant"] for r in rows}):
        lines.append(f"- **{v}**: {agg(v, None)}")
    (RUNS / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-8:]))


# ---------------- 跑完后自动压缩 Docker 数据盘 ----------------

COMPACT_TASK = "DockerVhdxCompact"


def compact_docker_after_run(wait_seconds: int = 0) -> None:
    """触发一次性的计划任务（管理员身份），把 VHDX 里的空洞还给系统。"""
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", COMPACT_TASK],
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        print(f"[compact] 触发 {COMPACT_TASK}: {'已启动' if ok else r.stdout.strip() or r.stderr.strip()}")
        if ok and wait_seconds:
            import time as _t
            _t.sleep(wait_seconds)
    except Exception as e:
        print(f"[compact] 跳过（任务未安装或不可用）: {type(e).__name__}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", default="")
    ap.add_argument("--variants", default="baseline,b")
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--gen-workers", type=int, default=2)
    ap.add_argument("--eval-workers", type=int, default=3)
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="已有 eval/report.json 的组合跳过")
    ap.add_argument("--dataset", default="", help="实例 parquet 路径（默认 SWE-bench Lite）")
    ap.add_argument("--purge-image", action="store_true", help="每题判定完就删镜像（省磁盘）")
    ap.add_argument("--no-compact", action="store_true", help="跑完不自动压缩 Docker 数据盘")
    a = ap.parse_args()

    global DATASET, PURGE_IMAGE
    PURGE_IMAGE = bool(a.purge_image)
    if a.dataset:
        DATASET = Path(a.dataset)
    RUNS.mkdir(exist_ok=True)
    if a.summary_only:
        summarize()
        return

    insts = load_instances()
    ids = PILOT if a.pilot else [s for s in a.instances.split(",") if s]
    variants = [v for v in a.variants.split(",") if v]
    jobs = [(i, v) for v in variants for i in ids if i in insts]
    if a.skip_done:
        before = len(jobs)
        jobs = [t for t in jobs if not (RUNS / t[1] / t[0] / "eval" / "report.json").exists()]
        print(f"跳过已完成 {before - len(jobs)} 个组合")
    print(f"实例 {len(ids)} 个 | 变体 {variants} | 待跑组合 {len(jobs)}")

    channel = CHANNEL_PRIMARY
    print("渠道探活…")
    if not probe_channel(channel):
        fb = channel_fallback()
        print(f"主渠道不可用，回退到 {fb['TRACE_BASE_URL']}（key 长度 {len(fb['TRACE_API_KEY'])}）")
        if probe_channel(fb):
            channel = fb
        else:
            print("⚠️ 两个渠道都不可用，仍按主渠道继续")
    print(f"使用渠道: {channel['TRACE_BASE_URL']} / {channel['TRACE_MODEL']}")

    with cf.ThreadPoolExecutor(max_workers=a.gen_workers) as ex:
        gen_results = list(ex.map(lambda t: gen_one(t[0], t[1], insts[t[0]], channel), jobs))
    ok = {(g["instance"], g["variant"]) for g in gen_results
          if g.get("gen_ok", True) and (g.get("patch_chars") or 0) > 0}
    eval_jobs = [t for t in jobs if (t[0], t[1]) in ok]
    skipped = [t for t in jobs if (t[0], t[1]) not in ok]
    for t in skipped:
        print(f"[skip] {t[1]:8} {t[0]:26} 生成阶段无有效补丁，跳过评测")
    with cf.ThreadPoolExecutor(max_workers=a.eval_workers) as ex:
        list(ex.map(lambda t: eval_one(t[0], t[1], insts[t[0]]), eval_jobs))
    summarize()
    if not a.no_compact:
        compact_docker_after_run()


if __name__ == "__main__":
    main()

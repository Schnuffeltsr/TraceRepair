# -*- coding: utf-8 -*-
"""测试驱动的文件定位（deterministic，无 LLM）。

思路：官方 FAIL_TO_PASS 里写着失败测试的路径/名字；测试文件本身会 import 被测模块。
所以「从测试反推实现文件」比「用 issue 关键词猜文件」可靠得多。

支持的 F2P 写法：
  astropy/coordinates/tests/test_x.py::test_a          → 直接是路径
  testing/test_reports.py::Cls::test_b                 → 直接是路径
  test_combine_media (forms_tests.tests.test_media.C)  → 点分模块 → 路径
  test_equality                                        → 裸函数名：全仓搜 `def test_equality(`
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

MAX_TEST_FILES = 6


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").lstrip("./")


def parse_test_paths(f2p_ids: list[str], repo_root: str) -> list[str]:
    """从 F2P id 里解析出测试文件（仓库相对路径），解析不到的用函数名搜索兜底。"""
    repo = Path(repo_root)
    found: list[str] = []
    bare: list[str] = []

    def add(p: str) -> None:
        p = _norm(p)
        if p and (repo / p).is_file() and p not in found:
            found.append(p)

    for rid in f2p_ids or []:
        s = str(rid).strip()
        if "::" in s:
            head = s.split("::")[0]
            if head.endswith(".py"):
                add(head)
            continue
        m = re.match(r"^\s*\w+\s*\((?:tests?\.)?([\w.]+)\)\s*$", s)   # test_x (a.b.C.method)
        if m:
            dotted = m.group(1)
            parts = dotted.split(".")
            got = False
            for cut in range(len(parts), 0, -1):
                cand = "/".join(parts[:cut]) + ".py"
                if (repo / cand).is_file():
                    add(cand); got = True; break
                suf = _suffix_file(repo, ".".join(parts[:cut]))
                if suf:
                    add(suf); got = True; break
            if not got and len(parts) > 1 and parts[-1][:1].isupper():
                suf = _suffix_file(repo, ".".join(parts[:-1]))
                if suf:
                    add(suf)
            continue
        bare.append(s)

    # 裸函数名/类名：全仓搜 def/class
    for name in bare[:8]:
        name = name.split(".")[0].strip()
        if not re.match(r"^[A-Za-z_]\w*$", name):
            continue
        pat = re.compile(rf"^\s*(?:def|class)\s+{re.escape(name)}\b", re.M)
        hits = 0
        for root, dirs, files in os.walk(repo):
            if ".git" in root or hits >= 6:
                continue
            for f in files:
                if not f.endswith(".py") or hits >= 6:
                    continue
                if not (f.startswith("test_") or f.endswith("_test.py")):
                    continue          # 裸名字只在测试文件里找，避免命中源码里的同名函数
                fp = Path(root) / f
                try:
                    txt = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if pat.search(txt):
                    add(str(fp.relative_to(repo)))
                    hits += 1
    return found[:MAX_TEST_FILES]


def _package_roots(repo: Path) -> list[Path]:
    roots = [repo]
    if (repo / "src").is_dir():
        roots.append(repo / "src")
    return roots


def _suffix_file(repo: Path, dotted: str) -> str | None:
    """仓库任意位置下匹配 xxx/yyy.py（用于 tests/ 前缀不定的项目，如 django）。"""
    tail = dotted.replace(".", "/")
    for cand in repo.glob(f"**/{tail}.py"):
        if cand.is_file():
            return _norm(str(cand.relative_to(repo)))
    return None


def _resolve_import(mod: str, level: int, test_rel: str, repo_root: str) -> str | None:
    repo = Path(repo_root)
    if level:                                     # 相对 import
        base = (repo / os.path.dirname(test_rel)).resolve()
        for _ in range(level - 1):
            base = base.parent
        targets = [base.joinpath(*mod.split("."))] if mod else [base]
    else:
        targets = [root.joinpath(*mod.split(".")) for root in _package_roots(repo)] if mod else []
    for target in targets:
        for cand in (target.with_suffix(".py"), target / "__init__.py"):
            try:
                rel = cand.resolve().relative_to(repo.resolve())
            except Exception:
                continue
            if cand.is_file():
                return _norm(str(rel))
    if mod and not level:
        return _suffix_file(repo, mod)
    return None


def _imports_of(test_rel: str, repo_root: str) -> list[str]:
    """测试文件里 import 到的仓库内模块（转成文件路径）。"""
    fp = Path(repo_root) / test_rel
    try:
        tree = ast.parse(fp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    out: list[str] = []
    repo = Path(repo_root)

    def _defines_in_dir(name: str, base_dir: Path) -> list[str]:
        """在 base_dir 下找 class/def <name>（用于 from pkg import Name 时定位真正定义处）。"""
        hits: list[str] = []
        pat = re.compile(rf"^\s*(?:class|def)\s+{re.escape(name)}\b", re.M)
        try:
            files = sorted(base_dir.glob("*.py"))
        except Exception:
            return hits
        for f in files[:80]:
            try:
                if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                    hits.append(_norm(str(f.relative_to(repo))))
                    if len(hits) >= 2:
                        break
            except Exception:
                continue
        return hits

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "." in mod:                      # from a.b import c  → a/b.py
                r = _resolve_import(mod, node.level, test_rel, repo_root)
                if r:
                    out.append(r)
                    # 再把 from a.b import Name 里的 Name 定位到真正定义它的文件
                    base_dir = (repo / os.path.dirname(r)).resolve()
                    for a in node.names:
                        nm = (a.name or "").split(".")[0]
                        if nm and nm != "*" and nm[:1].isalpha():
                            out.extend(_defines_in_dir(nm, base_dir))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if "." in a.name:
                    r = _resolve_import(a.name, 0, test_rel, repo_root)
                    if r:
                        out.append(r)
    return out


def is_test_file(rel: str) -> bool:
    low = _norm(rel).lower()
    base = os.path.basename(low)
    return "/tests/" in low or low.startswith("tests/") or base.startswith("test_") or base.endswith("_test.py")


def implementation_candidates(f2p_ids: list[str], repo_root: str, max_files: int = 4) -> dict:
    """返回 {'test_files': [...], 'candidates': [...]}：候选是**实现文件**（已剔除测试文件）。"""
    tests = parse_test_paths(f2p_ids, repo_root)
    score: dict[str, float] = {}
    for t in tests:
        for imp in _imports_of(t, repo_root):
            if is_test_file(imp):
                continue
            score[imp] = score.get(imp, 0.0) + 1.0
        # 同目录下的兄弟实现文件（测试目录的上一级）
        parent = os.path.dirname(os.path.dirname(_norm(t)))
        d = Path(repo_root) / parent
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                rel = _norm(str(f.relative_to(repo_root)))
                if is_test_file(rel) or f.name == "__init__.py":
                    continue
                score[rel] = score.get(rel, 0.0) + 0.1
    cands = [p for p, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))][:max_files]
    return {"test_files": tests, "candidates": cands, "scores": {k: round(v, 2) for k, v in score.items()}}

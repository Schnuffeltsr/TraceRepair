# -*- coding: utf-8 -*-
"""仓库结构图：文件级 import 关系 + 符号定义索引（纯 AST，确定性，可缓存）。

用途：给模型"这个文件依赖谁 / 谁依赖它 / 我要用的符号定义在哪"，补齐跨文件契约信息。
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

CACHE_NAME = ".trace_repograph.json"
SKIP_DIRS = {".git", "build", "dist", "node_modules", "__pycache__", ".tox", ".mypy_cache",
             "site-packages", ".eggs", "doc/_build", "docs/_build"}
MAX_FILES = 6000


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").lstrip("./")


def _iter_py(repo: Path):
    n = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, repo).replace("\\", "/")
        if any(part in SKIP_DIRS for part in rel_root.split("/")):
            continue
        for f in files:
            if f.endswith(".py"):
                yield _norm(os.path.join(rel_root, f)) if rel_root != "." else f
                n += 1
                if n >= MAX_FILES:
                    return


def _module_keys(rel: str) -> list[str]:
    """一个文件可能对应的 import 写法（含 src/ 前缀与包内相对写法）。"""
    p = rel[:-3] if rel.endswith(".py") else rel
    parts = [x for x in p.split("/") if x]
    keys = []
    if parts and parts[0] == "src":
        parts2 = parts[1:]
        if parts2:
            keys.append(".".join(parts2))
    keys.append(".".join(parts))
    # 包内子模块也可以被写成长路径的一部分，保留原样即可
    return [k for k in dict.fromkeys(keys) if k]


def _defs(tree: ast.AST) -> list[str]:
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
    return out


def _sig_line(tree: ast.AST, name: str, max_len: int = 160) -> str:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            try:
                src = ast.unparse(node)
            except Exception:
                return f"{name}(...)"
            first = src.splitlines()[0]
            return first[:max_len]
    return ""


def build_repo_graph(repo_root: str, use_cache: bool = True, verbose: bool = True) -> dict:
    """返回 {'imports': {f: [f]}, 'imported_by': {f: [f]}, 'symbols': {name: [f]}, 'sig': {f: {name: line}}}"""
    repo = Path(repo_root)
    cache = repo / CACHE_NAME
    if use_cache and cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    files = [rel for rel in _iter_py(repo)]
    mod2file: dict[str, str] = {}
    trees: dict[str, ast.AST] = {}
    for rel in files:
        try:
            src = (repo / rel).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except Exception:
            continue
        trees[rel] = tree
        for key in _module_keys(rel):
            mod2file.setdefault(key, rel)

    def resolve(mod: str, level: int, from_rel: str) -> str | None:
        if level:
            base = Path(from_rel).parent
            for _ in range(level - 1):
                if base.parent == base:
                    break
                base = base.parent
            cand = (base / Path(*mod.split("."))).as_posix() if mod else base.as_posix()
            for suffix in (".py", "/__init__.py"):
                if (repo / (cand + suffix)).is_file():
                    return _norm(cand + suffix)
            return None
        if not mod:
            return None
        hit = mod2file.get(mod)
        if hit:
            return hit
        # 前缀回退：a.b.c 找不到就试 a.b
        parts = mod.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            hit = mod2file.get(".".join(parts[:cut]))
            if hit:
                return hit
        return None

    imports: dict[str, list[str]] = {}
    imported_by: dict[str, list[str]] = {}
    symbols: dict[str, list[str]] = {}
    sig: dict[str, dict[str, str]] = {}

    for rel, tree in trees.items():
        deps: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                tgt = resolve(node.module or "", node.level, rel)
                if tgt and tgt != rel:
                    deps.append(tgt)
                # 名字定义处（精确到文件）
                for a in node.names:
                    nm = (a.name or "").split(".")[0]
                    if nm and nm != "*" and tgt:
                        base_dir = (repo / Path(tgt).parent).as_posix()
                        pat = re.compile(rf"^\s*(?:class|def)\s+{re.escape(nm)}\b", re.M)
                        try:
                            for f in sorted((repo / base_dir).glob("*.py"))[:80]:
                                r = _norm(f.relative_to(repo).as_posix())
                                if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                                    deps.append(r)
                                    break
                        except Exception:
                            pass
            elif isinstance(node, ast.Import):
                for a in node.names:
                    tgt = resolve(a.name, 0, rel)
                    if tgt and tgt != rel:
                        deps.append(tgt)
        imports[rel] = list(dict.fromkeys(deps))
        for d in imports[rel]:
            imported_by.setdefault(d, []).append(rel)
        names = _defs(tree)
        sig[rel] = {}
        for nm in names:
            symbols.setdefault(nm, []).append(rel)
            line = _sig_line(tree, nm)
            if line:
                sig[rel][nm] = line

    graph = {"imports": imports, "imported_by": imported_by, "symbols": symbols, "sig": sig}
    try:
        cache.write_text(json.dumps(graph), encoding="utf-8")
    except Exception:
        pass
    if verbose:
        print(f"🕸️ [RG] 图建好: {len(trees)} 文件, {sum(len(v) for v in imports.values())} 条依赖边")
    return graph


def _sigs_of(graph: dict, rel: str, limit: int = 3) -> str:
    s = graph.get("sig", {}).get(rel, {})
    items = list(s.values())[:limit]
    return "; ".join(items)


def neighbors_block(target_files: list[str], graph: dict, max_files: int = 8) -> str:
    """给目标文件生成"依赖 / 被依赖 / 符号定义处"的紧凑契约块。"""
    tg = [_norm(t) for t in target_files]
    imports = graph.get("imports", {})
    imported_by = graph.get("imported_by", {})
    symbols = graph.get("symbols", {})

    dep_lines, user_lines, sym_lines = [], [], []
    for t in tg:
        for d in imports.get(t, [])[:max_files]:
            dep_lines.append(f"  - {t} imports {d} :: {_sigs_of(graph, d, 2)}")
        for u in imported_by.get(t, [])[:max_files]:
            user_lines.append(f"  - {u} imports {t} :: {_sigs_of(graph, u, 2)}")
        base = Path(t).stem
        for nm in symbols:
            if nm.lower() == base.lower() or nm.lower() in base.lower():
                for f in symbols[nm][:2]:
                    if f != t:
                        sym_lines.append(f"  - `{nm}` also defined in {f} :: {_sigs_of(graph, f, 1)}")

    parts = ["[Repo structure (deterministic AST index; use it to keep cross-file contracts)]"]
    if dep_lines:
        parts.append("Dependencies of the files you may edit:\n" + "\n".join(dict.fromkeys(dep_lines)))
    if user_lines:
        parts.append("Files that import the files you may edit (do not break them):\n" +
                     "\n".join(dict.fromkeys(user_lines)))
    if sym_lines:
        parts.append("Symbols with similar names defined elsewhere (check before adding imports):\n" +
                     "\n".join(dict.fromkeys(sym_lines)))
    if len(parts) == 1:
        parts.append("(no cross-file relations found for these files)")
    return "\n\n".join(parts)

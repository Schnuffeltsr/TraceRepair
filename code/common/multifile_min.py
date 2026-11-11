# -*- coding: utf-8 -*-
"""最小版多文件支持（纯函数，确定性，可单测）。

只做三件事：
1. 从模型回复里解析候选文件路径，并与词法打分合并成有序候选集；
2. 组装"多文件 diff"提示词（白名单 + 每文件内容 + 输出契约）；
3. 把模型回复里**多个** diff 围栏合并成一个补丁。
不做：越界强制剔除、apply 校验、plan-first（这些是完整版）。
"""
from __future__ import annotations

import ast
import os
import re
from typing import Iterable

MAX_CHARS_PER_FILE = 12000      # 单文件内容上限（字符）
MAX_TOTAL_CHARS = 42000         # 多文件内容总上限（网关 50k 限制留余量）

PATH_RE = re.compile(r"[A-Za-z0-9_\-./\\]+\.py\b")


def norm(p: str) -> str:
    """统一成 forward-slash 的相对路径（Windows 上 os.path.relpath 会给反斜杠）。"""
    s = str(p).replace("\\", "/").strip()
    s = re.sub(r"/+", "/", s)
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def is_test_path(p: str) -> bool:
    low = norm(p).lower()
    base = os.path.basename(low)
    return ("/tests/" in low or low.startswith("tests/") or base.startswith("test_")
            or base.endswith("_test.py") or base == "conftest.py")


def parse_paths_from_reply(reply: str, limit: int = 8) -> list[str]:
    """从模型回复里抽出候选路径（支持 `path | 依据`、列表、反引号、行内路径）。"""
    if not reply:
        return []
    out: list[str] = []
    for line in str(reply).splitlines():
        head = line.split("|")[0].split("—")[0].split("(")[0]
        for m in PATH_RE.findall(head):
            p = m.strip().strip("`'\"").lstrip("./").replace("\\", "/")
            if p and p not in out:
                out.append(p)
            if len(out) >= limit:
                return out
    return out


def _issue_tokens(issue: str) -> list[str]:
    toks = set(re.findall(r"\b[A-Z]\w{2,}\b", issue))          # 类名/异常名
    toks.update(re.findall(r"\b([a-z_]\w{2,})\s*\(", issue))   # 函数调用
    toks.update(re.findall(r"\b([a-z]+_[a-z_]+)\b", issue))    # snake_case
    common = {"the", "and", "error", "issue", "test", "file", "python", "should",
              "when", "with", "this", "that", "code", "fix", "bug", "expected"}
    return [t for t in toks if t.lower() not in common and len(t) > 2]


def score_skeletons(issue: str, skeletons: dict[str, str]) -> dict[str, float]:
    """确定性词法打分：issue 标识符命中文件名 / 骨架 / 路径段；测试文件降权。"""
    toks = _issue_tokens(issue)
    scores: dict[str, float] = {}
    for rel, skel in skeletons.items():
        low_rel, low_skel = rel.lower(), (skel or "").lower()
        s = 0.0
        for t in toks:
            tl = t.lower()
            if tl in low_rel:
                s += 2.0
            if tl in low_skel:
                s += 1.0
        base = os.path.basename(low_rel)
        if any(seg in low_rel for seg in ("tests/", "test_", "_test.py", "/testing/")):
            s -= 1.0
        if base.startswith("__") or "conftest" in base:
            s -= 0.5
        scores[rel] = s
    return scores


def choose_candidates(llm_paths: Iterable[str], scored: dict[str, float],
                      existing: Iterable[str], k: int = 4) -> list[str]:
    """LLM 给的顺序优先（且必须真实存在），再用词法分数补齐；测试文件不进白名单。

    返回的路径统一为 forward slash。
    """
    exist = {norm(p) for p in existing}
    idx = {norm(p): p for p in existing}
    prod = [p for p in exist if not is_test_path(p)] or list(exist)

    out: list[str] = []

    def add(p: str) -> None:
        p = norm(p)
        if p in exist and p not in out and not is_test_path(p):
            out.append(p)

    for p in llm_paths:
        add(p)
    for rel, _score in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0])):
        add(rel)
    if not out:                                  # 全是测试文件时退回：至少给一个实现文件
        for rel, _score in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0])):
            r = norm(rel)
            if r in exist and r not in out:
                out.append(r)
                break
    _ = idx
    return [norm(p) for p in out[:k]]


def render_file_block(rel_path: str, content: str, cap: int = MAX_CHARS_PER_FILE) -> str:
    try:
        cap = int(os.environ.get("TRACE_MF_FILE_CAP", cap))
    except Exception:
        pass
    body = content or ""
    truncated = len(body) > cap
    if truncated:
        half = cap // 2
        body = (body[:half] + "\n\n# ... [TRUNCATED BY PIPELINE] ...\n\n" + body[-half:])
    return f'<file path="{rel_path}"{" truncated=\"true\"" if truncated else ""}>\n{body}\n</file>'


def build_multi_diff_prompt(issue_desc: str, files: list[tuple[str, str]],
                           graph_info: str = "", cap: int = MAX_CHARS_PER_FILE) -> str:
    """多文件 diff 生成提示。files 为 [(rel_path, content)]，第一个视为主文件。"""
    allowed = [p for p, _ in files]
    blocks, total = [], 0
    for rel, content in files:
        b = render_file_block(rel, content, cap=cap)
        if total + len(b) > MAX_TOTAL_CHARS and blocks:
            blocks.append('<file path="%s" omitted="true">(omitted by budget)</file>' % rel)
            continue
        blocks.append(b)
        total += len(b)
    return f"""You are a Principal Software Engineer fixing a GitHub issue across MULTIPLE files.

<issue>
{issue_desc}
</issue>

<allowed_files>
{chr(10).join('- ' + p for p in allowed)}
</allowed_files>

<files>
{chr(10).join(blocks)}
</files>

<traceability_context>
{graph_info or '(none)'}
</traceability_context>

<rules>
1. Output ONE ```diff fenced block. Inside it, emit one `diff --git a/<path> b/<path>` section per file you change.
2. Only modify files listed in <allowed_files>. Never create, delete or rename files. Never touch test files.
3. Every hunk must include enough unchanged context lines (3-5) copied verbatim from the file content so the patch applies.
4. Do not invent line numbers: derive them from the given content; context lines matter more than numbers.
5. Keep changes minimal and consistent across files (same names, signatures, imports).
6. End the block with the line `# END PATCH` so truncation can be detected.
</rules>
"""


def merge_diff_blocks(response: str) -> str:
    """把回复里所有 diff 段合并成一个补丁（模型常按文件分成多个围栏）。"""
    if not response:
        return ""
    text = str(response).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.findall(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL)
    picked = [b for b in blocks if ("diff --git" in b or "@@" in b)]
    if picked:
        merged = "\n".join(b.rstrip() for b in picked)
    elif "diff --git" in text:
        merged = text[text.find("diff --git"):]
    else:
        merged = ""
    # 去掉可能夹带的 END 标记与多余空行
    merged = re.sub(r"^\s*#\s*END PATCH\s*$", "", merged, flags=re.M).strip()
    return (merged + "\n") if merged else ""


def scope_violations(patch: str, allowed: Iterable[str]) -> list[str]:
    """返回补丁里越界（不在白名单）的文件路径；最小版只用于打印告警。"""
    allow = {norm(a) for a in allowed}
    found = re.findall(r"^diff --git a/(\S+) b/\S+", patch or "", flags=re.M)
    return [f for f in found if norm(f) not in allow]


# ---------------- 安全网（mf2 用） ----------------

def enforce_scope(patch: str, allowed: Iterable[str]) -> tuple[str, list[str]]:
    """把补丁裁到白名单内：越界的 `diff --git` 段整段删除。返回 (新补丁, 被删路径)。"""
    allow = {norm(a) for a in allowed}
    lines = (patch or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    removed: list[str] = []
    current_path: str | None = None
    dropping = False
    for line in lines:
        m = re.match(r"^diff --git a/(\S+) b/\S+", line)
        if m:
            current_path = norm(m.group(1))
            dropping = current_path not in allow
            if dropping:
                removed.append(current_path)
                continue
        kept.append(line)
    out = "\n".join(kept).strip()
    return (out + "\n") if out else "", removed


def module_name_from_path(rel_path: str) -> str:
    """把仓库相对路径转成 import 用的点分模块名（跳过 __init__ 与非法标识符段）。"""
    p = norm(rel_path)
    if p.endswith(".py"):
        p = p[:-3]
    parts = [x for x in p.split("/") if x]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if any(not re.match(r"^[A-Za-z_]\w*$", x) for x in parts):
        return ""
    return ".".join(parts)


def _module_file_candidates(module: str, patch_path: str, repo_root: str) -> list[str]:
    """给出模块可能对应的文件（绝对/相对 import 都试）。"""
    cands: list[str] = []
    base = os.path.dirname(norm(patch_path))
    if module.startswith("."):
        depth = len(module) - len(module.lstrip("."))
        rest = module[depth:]
        up = "/".join([".."] * (depth - 1)) if depth > 1 else "."
        rel = os.path.normpath(os.path.join(base, up, *rest.split("."))) if rest else os.path.normpath(os.path.join(base, up))
        rel = rel.replace("\\", "/").lstrip("./")
        cands += [os.path.join(repo_root, rel + ".py"), os.path.join(repo_root, rel, "__init__.py")]
    else:
        rel = module.replace(".", "/")
        cands += [os.path.join(repo_root, rel + ".py"), os.path.join(repo_root, rel, "__init__.py")]
    return cands


def _defined_names(path: str) -> set[str] | None:
    try:
        tree = ast.parse(open(path, "r", encoding="utf-8", errors="replace").read())
    except Exception:
        return None
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def ast_import_violations(patch: str, repo_root: str) -> list[str]:
    """检查补丁新增的 `from X import a, b`：a/b 是否真的在 X 里定义（拦幻觉 import）。"""
    problems: list[str] = []
    cur_file = ""
    for line in (patch or "").split("\n"):
        m = re.match(r"^diff --git a/(\S+) b/\S+", line)
        if m:
            cur_file = norm(m.group(1))
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        m2 = re.match(r"^from\s+(\.[\w.]*|[\w.]+)\s+import\s+\(?([\w\s,.*]+)\)?$", body)
        if not m2:
            continue
        mod, names = m2.group(1), [n.strip().split(" as ")[0] for n in m2.group(2).split(",")]
        names = [n for n in names if n and n != "*"]
        if not names:
            continue
        defined = None
        for cand in _module_file_candidates(mod, cur_file, repo_root):
            if os.path.isfile(cand):
                defined = _defined_names(cand)
                if defined is not None:
                    break
        if defined is None:
            continue
        missing = [n for n in names if n not in defined]
        if missing:
            problems.append(f"{cur_file}: from {mod} import {', '.join(missing)} —— 目标模块里没有这些名字")
    return problems


def import_smoke_targets(patch: str) -> list[str]:
    """补丁触及文件的点分模块名（给容器里的 import smoke 用）。"""
    out: list[str] = []
    for f in re.findall(r"^diff --git a/(\S+) b/\S+", patch or "", flags=re.M):
        m = module_name_from_path(f)
        if m and m not in out:
            out.append(m)
    return out

# ---------------- plan-first（mf3 用） ----------------

def build_plan_prompt(issue_desc, file_map, graph_info="", max_skel_chars=2500):
    """第一步：只做计划（不写代码）。file_map: {rel_path: skeleton_or_content}。"""
    blocks = []
    for rel, sk in file_map.items():
        s = (sk or "")[:max_skel_chars]
        blocks.append('<file path="%s">\n%s\n</file>' % (rel, s))
    return """You are a Principal Software Engineer planning a fix across multiple files.
Do NOT write a patch yet. Produce a CHANGE PLAN only.

<issue>
%s
</issue>

<files_skeleton>
%s
</files_skeleton>

<traceability_context>
%s
</traceability_context>

<output_format>
Return ONE JSON array (no prose), e.g.:
[
  {"file": "pkg/a.py", "intent": "add rotation_matrix import", "symbols": ["rotation_matrix"], "why": "needed by the new transform"},
  {"file": "pkg/b.py", "intent": "call the new helper", "symbols": ["itrs_to_observed_mat"], "why": "transform must use the new matrix"}
]
Rules: only files from <files_skeleton>; 1-6 items; symbols must be names that already exist
or that another plan item creates; keep the plan minimal (no cosmetic edits).
</output_format>
""" % (issue_desc, "\n".join(blocks), graph_info or "(none)")


def parse_plan_json(text):
    """从模型回复里抽出计划 JSON（容错：围栏、前后解释、单引号）。"""
    import json as _json
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    raw = m.group(1) if m else text
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    body = raw[start:end + 1]
    try:
        data = _json.loads(body)
    except Exception:
        try:
            data = _json.loads(body.replace("'", '"'))
        except Exception:
            return []
    out = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("file"):
                out.append({"file": norm(it["file"]),
                            "intent": str(it.get("intent", ""))[:300],
                            "symbols": [str(s) for s in (it.get("symbols") or [])][:8],
                            "why": str(it.get("why", ""))[:200]})
    return out


def _join(items):
    try:
        return ", ".join(str(x) for x in items)
    except Exception:
        return ""


def build_per_file_prompt(rel_path, content, plan_item, plan_all, graph_info="", cap=MAX_CHARS_PER_FILE):
    """第二步：只为一个文件生成 diff，但带着完整计划（保证跨文件一致）。"""
    plan_txt = "\n".join('- %s: %s (%s)' % (p["file"], p["intent"], _join(p.get("symbols", []))) for p in plan_all)
    body = content or ""
    if len(body) > cap:
        half = cap // 2
        body = body[:half] + "\n# ... [TRUNCATED] ...\n" + body[-half:]
    return """You are implementing ONE step of an agreed change plan.

<plan>
%s
</plan>

<this_step file="%s">
intent: %s
symbols: %s
why: %s
</this_step>

<file_content path="%s">
%s
</file_content>

<traceability_context>
%s
</traceability_context>

<rules>
1. Output ONE ```diff fenced block that changes ONLY `%s`.
2. Use `diff --git a/%s b/%s` plus `---`/`+++` headers.
3. Include 3-5 lines of verbatim unchanged context per hunk so the patch applies.
4. Implement exactly the intent above; do not rename unrelated symbols; no test files.
5. End the block with `# END PATCH`.
</rules>
""" % (plan_txt, rel_path, plan_item.get("intent", ""), _join(plan_item.get("symbols", [])),
           plan_item.get("why", ""), rel_path, body, graph_info or "(none)",
           rel_path, rel_path, rel_path)


def merge_per_file_patches(patches):
    """把逐文件生成的补丁按顺序拼接（去掉空段）。"""
    parts = [p.strip() for p in patches if p and p.strip()]
    return ("\n".join(parts) + "\n") if parts else ""

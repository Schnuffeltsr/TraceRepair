# -*- coding: utf-8 -*-
"""从 smoke_trace_repair.py 生成最小版多文件变体 trace_repair_mf.py（原文件不动）。

改动：
1. 引入 multifile_min 的纯函数；
2. 新增 identify_target_files_multi / generate_multi_patch_with_llm / generate_fixed_multi_patch_with_llm；
3. main() 里"定位 + 生成"改成多文件优先、单文件兜底；
4. 重试轮改成多文件修订。
"""
from pathlib import Path

RUN = Path(r"D:\comuatedp\trace_run")
SRC = RUN / "smoke_trace_repair.py"
DST = RUN / "trace_repair_mf.py"

text = SRC.read_text(encoding="utf-8")
edits: list[str] = []


def rep(old: str, new: str, tag: str) -> None:
    global text
    assert old in text, f"锚点未找到: {tag}"
    text = text.replace(old, new, 1)
    edits.append(tag)


# ---- 1) 引入 multifile_min ----
rep(
    "import difflib\n",
    """import difflib

# ===== [MF] 最小版多文件支持（纯函数模块） =====
import sys as _mf_sys
_mf_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multifile_min import (parse_paths_from_reply, score_skeletons, choose_candidates,
                           build_multi_diff_prompt, merge_diff_blocks, scope_violations)
MF_K = int(os.environ.get('TRACE_MF_K', '4'))          # 候选文件数上限
MF_ENABLED = os.environ.get('TRACE_MF', '1') == '1'    # 关掉即退回单文件
""",
    "import multifile_min",
)

# ---- 2) 新增多文件函数（插在 def main 之前） ----
NEW_FUNCS = '''
# ================= [MF] 最小版多文件支持 =================
def identify_target_files_multi(issue_description, repo_path, k=MF_K):
    """返回**有序**候选文件列表（LLM 顺序优先，再用词法分补齐）。失败返回 []。"""
    import warnings
    from multifile_min import _issue_tokens  # noqa: F401  (保持接口一致)

    skeletons = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for root_dir, _dirs, files_in_dir in os.walk(repo_path):
            if '.git' in root_dir:
                continue
            for f in files_in_dir:
                if f.endswith('.py'):
                    full = os.path.join(root_dir, f)
                    rel = os.path.relpath(full, repo_path).replace(chr(92), '/')
                    sk = get_file_skeleton(full)
                    if sk:
                        skeletons[rel] = sk

    if not skeletons:
        return []

    scored = score_skeletons(issue_description, skeletons)
    # 只把分数前 60 的骨架发给模型（控制提示长度）
    top = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:60]
    ctx = "\\n".join(f"File: {rel}\\n   {skeletons[rel]}" for rel, _ in top)

    system_prompt = ("You are a Principal Software Architect. Identify EVERY file that must be modified "
                     "to fix the issue (a fix may span multiple files).")
    user_prompt = f"""
========== Issue Description ==========
{issue_description}

========== Repository Skeleton (Classes & Functions) ==========
{ctx}

========== Task ==========
Return up to {k} files that most likely need modification, ordered by confidence.
Format: one path per line, then " | " and a very short reason. Example:
pkg/module.py | defines the failing function
Only repository-relative paths that appear above.
"""
    try:
        reply, _ = test2_ask_question(chat_content=system_prompt + "\\n" + user_prompt, model_name='gpt-5.4')
    except Exception as e:
        print(f"❌ [MF] 多文件定位调用失败: {e}")
        return []

    llm_paths = parse_paths_from_reply(reply or "")
    files = choose_candidates(llm_paths, scored, skeletons.keys(), k=k)
    print(f"🧭 [MF] LLM 候选: {llm_paths[:k]} | 最终白名单({len(files)}): {files}")
    return files


def generate_multi_patch_with_llm(file_pairs, issue_desc, demand_graph_info, model_name='gpt-5.4'):
    """一次调用生成多文件 diff；把回复里多个围栏合并成一个补丁。"""
    graph_info = json.dumps(demand_graph_info, ensure_ascii=False) if demand_graph_info else ""
    prompt = build_multi_diff_prompt(issue_desc, file_pairs, graph_info=graph_info)
    print(f"🤖 [MF] 请求生成多文件 diff（{len(file_pairs)} 个文件，提示 {len(prompt)} 字符）...")
    response, _ = test2_ask_question(chat_content=prompt, model_name=model_name)
    if not response or not response.strip():
        return "", "Empty response returned by model.", response or ""
    patch = merge_diff_blocks(response)
    if not patch.strip():
        return "", "Failed to merge diff blocks from response.", response
    bad = scope_violations(patch, [p for p, _ in file_pairs])
    if bad:
        print(f"⚠️ [MF] 补丁触碰了白名单外的文件（最小版仅告警）: {bad}")
    return patch, "", response


def generate_fixed_multi_patch_with_llm(file_pairs, issue_desc, previous_patch, error_log,
                                        error_analysis, model_name='gpt-5.4'):
    """修订轮：白名单文件 + 裁剪后的错误日志 + 上一轮补丁。"""
    log = (error_log or "")[-6000:]
    blocks = []
    budget = 30000
    for rel, content in file_pairs:
        body = content if len(content) <= 9000 else (content[:4500] + "\\n# ... [TRUNCATED] ...\\n" + content[-4500:])
        blk = f'<file path="{rel}">\\n{body}\\n</file>'
        if budget - len(blk) < 0 and blocks:
            blocks.append(f'<file path="{rel}" omitted="true">(omitted by budget)</file>')
            continue
        blocks.append(blk)
        budget -= len(blk)
    prompt = f"""You are a Principal Software Engineer. The previous patch failed the tests.

<issue>
{issue_desc}
</issue>

<files>
{chr(10).join(blocks)}
</files>

<previous_patch>
{previous_patch}
</previous_patch>

<error_log>
{log}
</error_log>

<error_analysis>
{error_analysis}
</error_analysis>

<rules>
1. Output ONE ```diff fenced block with one `diff --git a/<path> b/<path>` section per changed file.
2. Only modify the files listed under <files>. Never create/delete/rename files. Never touch test files.
3. Include 3-5 lines of verbatim context per hunk so `git apply` succeeds.
4. Fix the root cause, not the tests. End the block with `# END PATCH`.
</rules>
"""
    response, _ = test2_ask_question(chat_content=prompt, model_name=model_name)
    if not response or not response.strip():
        return "", "Empty response returned by model.", response or ""
    patch = merge_diff_blocks(response)
    if not patch.strip():
        return "", "Failed to merge diff blocks from response.", response
    bad = scope_violations(patch, [p for p, _ in file_pairs])
    if bad:
        print(f"⚠️ [MF] 修订补丁触碰白名单外文件: {bad}")
    return patch, "", response


'''
rep("def main():", NEW_FUNCS + "def main():", "insert multi-file functions")

# ---- 3) main(): 定位 + 生成 改成多文件优先 ----
OLD_BLOCK = """            if repo_path:
                # 【修改】内部的 identify_target_file 已经指定了 model_name='gpt-5.4'
                rel_file_path, full_file_path, is_fallback = repo_manager.identify_target_file(prompt, repo_path)

                if is_fallback:
                    print(f"⚠️ 题目 {task_id} 触发了终极保底文件定位，跳过代码生成，不计入总题数。")
                    eval_outcome = {"status": "fallback_skipped",
                                    "reason": "Triggered ultimate fallback for file identification"}
                    effective_total_tasks -= 1
                elif rel_file_path and full_file_path:
                    with open(full_file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()

                    # 【修改】调用新的 Diff 生成函数，明确指定使用 gpt-5.4
                    model_patch, error_msg, llm_raw = generate_patch_with_llm(
                        rel_file_path=rel_file_path,
                        issue_desc=prompt,
                        file_content=file_content,
                        demand_graph_info=demand_graph_info,
                        model_name='gpt-5.4'
                    )

                    if not model_patch:
                        print(f"⚠️ 未能生成有效的 Patch。")
                        print(f"🛑 [失败原因]: {error_msg}")
                        print(f"🛑 [大模型原始回复内容]:\\n{llm_raw}\\n{'-' * 40}")
                        # 按照你的要求，保留 sys.exit(1) 暂停程序以供排查
                        sys.exit(1)
                    else:
                        # 成功生成 Patch，直接保存
                        GraphVisualizer.save_text_report(model_patch, "final_patch.diff")
"""

NEW_BLOCK = """            mf_pairs = []
            if repo_path:
                # ===== [MF] 最小版多文件：先定位候选文件集，再一次性生成多文件 diff =====
                if MF_ENABLED:
                    try:
                        mf_files = identify_target_files_multi(prompt, repo_path, k=MF_K)
                    except Exception as _e:
                        print(f"⚠️ [MF] 多文件定位异常，回退单文件：{_e}")
                        mf_files = []
                    for _rp in mf_files:
                        try:
                            with open(os.path.join(repo_path, _rp.replace('/', os.sep)), 'r', encoding='utf-8') as _f:
                                mf_pairs.append((_rp, _f.read()))
                        except Exception:
                            continue

                if mf_pairs:
                    rel_file_path, file_content = mf_pairs[0][0], mf_pairs[0][1]
                    full_file_path = os.path.join(repo_path, rel_file_path)
                    print(f"📚 [MF] 多文件白名单 {len(mf_pairs)} 个: {[p for p, _ in mf_pairs]}")
                    model_patch, error_msg, llm_raw = generate_multi_patch_with_llm(
                        file_pairs=mf_pairs,
                        issue_desc=prompt,
                        demand_graph_info=demand_graph_info,
                        model_name='gpt-5.4',
                    )
                else:
                    # ---- 单文件兜底（原逻辑） ----
                    rel_file_path, full_file_path, is_fallback = repo_manager.identify_target_file(prompt, repo_path)
                    if is_fallback:
                        print(f"⚠️ 题目 {task_id} 触发了终极保底文件定位，跳过代码生成，不计入总题数。")
                        eval_outcome = {"status": "fallback_skipped",
                                        "reason": "Triggered ultimate fallback for file identification"}
                        effective_total_tasks -= 1
                    elif rel_file_path and full_file_path:
                        with open(full_file_path, 'r', encoding='utf-8') as f:
                            file_content = f.read()
                        mf_pairs = [(rel_file_path, file_content)]
                        model_patch, error_msg, llm_raw = generate_patch_with_llm(
                            rel_file_path=rel_file_path,
                            issue_desc=prompt,
                            file_content=file_content,
                            demand_graph_info=demand_graph_info,
                            model_name='gpt-5.4'
                        )

                if eval_outcome.get("status") != "fallback_skipped":
                    if not model_patch:
                        print(f"⚠️ 未能生成有效的 Patch。")
                        print(f"🛑 [失败原因]: {error_msg}")
                        print(f"🛑 [大模型原始回复内容]:\\n{llm_raw}\\n{'-' * 40}")
                        sys.exit(1)
                    else:
                        GraphVisualizer.save_text_report(model_patch, "final_patch.diff")
"""
rep(OLD_BLOCK, NEW_BLOCK, "multi-file main block")

# ---- 4) 重试轮改成多文件修订 ----
OLD_RETRY = """                        # 【修改】让大模型结合报错信息重新生成代码，明确指定使用 gpt-5.4
                        new_patch, error_msg, llm_raw = generate_fixed_patch_with_llm(
                            rel_file_path=rel_file_path,
                            issue_desc=prompt,
                            file_content=file_content,
                            previous_patch=model_patch,
                            error_log=error_log,
                            error_analysis=error_analysis,
                            model_name='gpt-5.4'
                        )"""
NEW_RETRY = """                        # 【MF】多文件修订：白名单文件 + 裁剪后的错误日志
                        new_patch, error_msg, llm_raw = generate_fixed_multi_patch_with_llm(
                            file_pairs=mf_pairs or [(rel_file_path, file_content)],
                            issue_desc=prompt,
                            previous_patch=model_patch,
                            error_log=error_log,
                            error_analysis=error_analysis,
                            model_name='gpt-5.4'
                        )"""
rep(OLD_RETRY, NEW_RETRY, "multi-file retry call")

DST.write_text(text, encoding="utf-8")
print(f"written: {DST} ({DST.stat().st_size} B)")
print(f"edits: {len(edits)} -> " + ", ".join(edits))

import sys
import os
import json
import contextlib
import time
import tempfile
import traceback
import re
import hashlib
import io
import ast
import subprocess
import glob
import shutil
import functools
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


# ================= Windows 兼容性补丁 (保持不变) =================
if sys.platform.startswith('win'):
    print("⚠️ 检测到 Windows 环境，正在应用 'resource' 和 'fcntl' 模块补丁...")


    class MockResource:
        RLIMIT_AS = 9;
        RLIMIT_CPU = 0;
        RLIMIT_DATA = 2;
        RLIMIT_FSIZE = 1
        RLIMIT_NOFILE = 7;
        RLIMIT_STACK = 3

        def getrlimit(self, resource): return (1024, 1024)

        def setrlimit(self, resource, limits): pass

        def getrusage(self, who): return None


    sys.modules['resource'] = MockResource()
    if 'fcntl' not in sys.modules:
        class MockFcntl:
            def ioctl(self, fd, op, arg=0, mutable_flag=True): return 0

            def flock(self, fd, op): pass

            def lockf(self, fd, operation, length=0, start=0, whence=0): pass

            LOCK_EX = 2;
            LOCK_SH = 1;
            LOCK_NB = 4;
            LOCK_UN = 8


        sys.modules['fcntl'] = MockFcntl()
# ====================================================================

import pandas as pd
import numpy as np
import networkx as nx
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
import nltk
from typing import Set, Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
try:
    from datasets import load_dataset
except Exception as _ds_err:  # [SMOKE] parquet is read directly; datasets may be broken/missing
    load_dataset = None
    print('datasets 不可用（已忽略）:', type(_ds_err).__name__)
from openai import OpenAI

# 指定中文字体
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

# ============================================================================
# 0. 全局配置与输出目录
# ============================================================================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 使用全新的输出文件夹，适配 300 题测试
BASE_OUTPUT_DIR = os.environ.get("TRACE_OUT_DIR", r"D:\comuatedp\trace_run\out")

# 指向你本地已经下载好的仓库根目录
LOCAL_REPO_BASE = os.environ.get("TRACE_REPO_BASE", r"D:\comuatedp\trace_run\repos")

CONFIG = {
    "vis_output_dir": BASE_OUTPUT_DIR,  # 默认值，将在循环中动态修改
    "repo_dir": LOCAL_REPO_BASE
}

if not os.path.exists(BASE_OUTPUT_DIR):
    os.makedirs(BASE_OUTPUT_DIR)

# 存储 300 题原始数据的文件夹
FULL_DATA_DIR = os.path.join(BASE_OUTPUT_DIR, "dataset_items_300")
if not os.path.exists(FULL_DATA_DIR):
    os.makedirs(FULL_DATA_DIR)

# 检查本地仓库目录是否存在
if not os.path.exists(CONFIG['repo_dir']):
    print(f"⚠️ 警告: 本地仓库目录不存在: {CONFIG['repo_dir']}")
else:
    print(f"📂 本地仓库根目录: {CONFIG['repo_dir']}")
    print(f"📂 全新基础输出目录: {BASE_OUTPUT_DIR}")
    print(f"📂 题目数据目录: {FULL_DATA_DIR}")

# ============================================================================
# 1. 可视化与文件保存工具类 (完全保留原有逻辑)
# ============================================================================
from matplotlib.path import Path


class GraphVisualizer:
    @staticmethod
    def tree_layout_topological(G_input):
        if isinstance(G_input, dict):
            G = nx.DiGraph(G_input)
        else:
            G = G_input

        try:
            topo_order = list(nx.topological_sort(G))
        except:
            topo_order = list(G.nodes())

        node_to_layer = {}
        for node in topo_order:
            predecessors = list(G.predecessors(node))
            if not predecessors:
                node_to_layer[node] = 0
            else:
                node_to_layer[node] = max(node_to_layer.get(pred, 0) for pred in predecessors) + 1

        if node_to_layer:
            max_layer = max(node_to_layer.values())
            for node in G.nodes():
                if G.out_degree(node) == 0 and G.in_degree(node) > 0:
                    node_to_layer[node] = max_layer

        layers = {}
        for node, layer in node_to_layer.items():
            if layer not in layers: layers[layer] = []
            layers[layer].append(node)

        pos = {}
        y_spacing = 10.0
        x_spacing = 14.0

        for level, nodes in layers.items():
            y = -level * y_spacing
            width = len(nodes) * x_spacing
            start_x = -(width / 2) + (x_spacing / 2)
            for i, node in enumerate(nodes):
                x = start_x + i * x_spacing
                pos[node] = (x, y)
        return pos, layers

    @staticmethod
    def create_bezier_path(p1, p2, rad):
        x1, y1 = p1
        x2, y2 = p2
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = x2 - x1, y2 - y1
        cx = mx + rad * dy / 2
        cy = my - rad * dx / 2
        verts = [(x1, y1), (cx, cy), (x2, y2)]
        codes = [Path.MOVETO, Path.CURVE3, Path.CURVE3]
        return Path(verts, codes), (cx, cy)

    @staticmethod
    def draw_bezier_edge(ax, p1, p2, rad, color='gray', linestyle='solid', alpha=0.5, width=1.5, arrow=True):
        path, (cx, cy) = GraphVisualizer.create_bezier_path(p1, p2, rad)
        arrow_style = '-|>' if arrow else '-'
        patch = mpatches.FancyArrowPatch(
            path=path, arrowstyle=arrow_style, color=color, linewidth=width,
            linestyle=linestyle, alpha=alpha, mutation_scale=20,
            shrinkA=15, shrinkB=15, zorder=10
        )
        ax.add_patch(patch)
        x1, y1 = p1
        x2, y2 = p2
        mid_x = 0.25 * x1 + 0.5 * cx + 0.25 * x2
        mid_y = 0.25 * y1 + 0.5 * cy + 0.25 * y2
        return mid_x, mid_y

    @staticmethod
    def save_graph_complete(G: nx.DiGraph, title: str, filename_suffix: str):
        if G.number_of_nodes() == 0: return
        fig, ax = plt.subplots(figsize=(20, 16))
        pos, _ = GraphVisualizer.tree_layout_topological(G)
        ARC_RAD = 0.2
        nx.draw_networkx_edges(G, pos, ax=ax, arrowstyle='-|>', arrowsize=20, edge_color='gray', width=1.5,
                               node_size=3500, connectionstyle=f'arc3,rad={ARC_RAD}')
        root_nodes = [n for n, d in G.in_degree() if d == 0]
        other_nodes = [n for n in G.nodes() if n not in root_nodes]

        if other_nodes:
            nx.draw_networkx_nodes(G, pos, ax=ax, nodelist=other_nodes, node_color='lightblue', node_size=3500,
                                   alpha=0.9, edgecolors='#333')
        if root_nodes:
            nx.draw_networkx_nodes(G, pos, ax=ax, nodelist=root_nodes, node_color='#87CEFA', node_size=3500, alpha=1.0,
                                   edgecolors='#000', linewidths=2.0)

        nx.draw_networkx_labels(G, pos, ax=ax, font_size=10, font_weight='bold',
                                bbox=dict(facecolor='white', alpha=0.9, edgecolor='none', pad=0.5))
        ax.set_title(title, fontsize=18, pad=20)
        ax.axis('off')

        img_path = os.path.join(CONFIG['vis_output_dir'], f"{filename_suffix}.png")
        try:
            fig.savefig(img_path, bbox_inches='tight', dpi=150)
            print(f"💾 [保存] 图片成功: {img_path}")
        except Exception:
            pass
        plt.close(fig)

        txt_path = os.path.join(CONFIG['vis_output_dir'], f"{filename_suffix}_data.txt")
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(f"Title: {title}\nNodes: {list(G.nodes())}\nEdges:\n")
            for u, v in G.edges(): f.write(f"  {u} -> {v}\n")

    @staticmethod
    def save_text_report(text: str, filename_suffix: str):
        save_path = os.path.join(CONFIG['vis_output_dir'], f"{filename_suffix}.txt")
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass

    @staticmethod
    def save_change_comparison(old_edges, new_graph, task_id):
        old_edge_set = set(tuple(e) for e in old_edges)
        new_edge_set = set(new_graph.edges())
        kept_edges = list(old_edge_set & new_edge_set)
        added_edges = list(new_edge_set - old_edge_set)
        removed_edges = list(old_edge_set - new_edge_set)

        all_nodes = set()
        for u, v in old_edge_set: all_nodes.add(u); all_nodes.add(v)
        all_nodes.update(new_graph.nodes())

        G_super = nx.DiGraph()
        G_super.add_nodes_from(all_nodes)

        pos = nx.spring_layout(G_super, k=3.0, seed=42)
        fig, axes = plt.subplots(1, 2, figsize=(28, 14))
        ARC_RAD = 0.2

        def draw_subgraph(ax, nodes, edges_kept, edges_diff, diff_color, diff_label):
            if edges_kept: nx.draw_networkx_edges(G_super, pos, ax=ax, edgelist=edges_kept, edge_color='gray',
                                                  arrowstyle='-|>', width=1.5, connectionstyle=f'arc3,rad={ARC_RAD}')
            if edges_diff: nx.draw_networkx_edges(G_super, pos, ax=ax, edgelist=edges_diff, edge_color=diff_color,
                                                  arrowstyle='-|>', width=2.0, style='dashed', label=diff_label,
                                                  connectionstyle=f'arc3,rad={ARC_RAD}')
            if nodes:
                nx.draw_networkx_nodes(G_super, pos, ax=ax, nodelist=nodes, node_color='lightblue', node_size=3000)
                nx.draw_networkx_labels(G_super, pos, ax=ax, labels={n: n for n in nodes}, font_size=9,
                                        bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=0.3))

        axes[0].set_title("Before Change");
        axes[1].set_title("After Change")
        draw_subgraph(axes[0], list(all_nodes), kept_edges, removed_edges, 'red', 'Removed')
        draw_subgraph(axes[1], list(all_nodes), kept_edges, added_edges, 'green', 'Added')

        filename = f"change_comparison_{task_id.replace('/', '_')}.png"
        plt.savefig(os.path.join(CONFIG['vis_output_dir'], filename), bbox_inches='tight')
        plt.close(fig)

    @staticmethod
    def save_final_trace_graph(G_demand, G_code, trace_connections, filename_suffix):
        print(f"🎨 正在绘制符合目标格式的因果追溯图...")
        pos_demand_local, _ = GraphVisualizer.tree_layout_topological(G_demand)
        pos_code_local, _ = GraphVisualizer.tree_layout_topological(G_code)

        final_pos_demand = {}
        min_y_demand = 0
        if pos_demand_local:
            min_y_demand = min(y for x, y in pos_demand_local.values())
            for node, (x, y) in pos_demand_local.items(): final_pos_demand[node] = (x, y)

        final_pos_code = {}
        max_y_code = 0
        if pos_code_local: max_y_code = max(y for x, y in pos_code_local.values())

        SECTION_GAP = 25.0
        y_shift = min_y_demand - max_y_code - SECTION_GAP

        for node, (x, y) in pos_code_local.items(): final_pos_code[node] = (x, y + y_shift)

        fig, ax = plt.subplots(figsize=(32, 26))
        EDGE_RAD = 0.2

        for u, v in G_code.edges():
            if u in final_pos_code and v in final_pos_code:
                GraphVisualizer.draw_bezier_edge(ax, final_pos_code[u], final_pos_code[v], EDGE_RAD, color='black',
                                                 alpha=0.5, width=1.5)

        for u, v in G_demand.edges():
            if u in final_pos_demand and v in final_pos_demand:
                GraphVisualizer.draw_bezier_edge(ax, final_pos_demand[u], final_pos_demand[v], EDGE_RAD, color='black',
                                                 alpha=0.5, width=1.5)

        for (d_u, d_v), t_type, t_val in trace_connections:
            if d_u not in final_pos_demand or d_v not in final_pos_demand: continue
            p1 = final_pos_demand[d_u];
            p2 = final_pos_demand[d_v]
            _, (cx, cy) = GraphVisualizer.create_bezier_path(p1, p2, EDGE_RAD)
            start_x = 0.25 * p1[0] + 0.5 * cx + 0.25 * p2[0]
            start_y = 0.25 * p1[1] + 0.5 * cy + 0.25 * p2[1]
            ax.plot(start_x, start_y, 'o', color='#444', markersize=6, zorder=25)

            end_x, end_y = 0, 0;
            valid_target = False
            if t_type == 'NODE':
                c_node = t_val
                if c_node in final_pos_code:
                    end_x, end_y = final_pos_code[c_node];
                    valid_target = True
            elif t_type == 'EDGE':
                c_u, c_v = t_val
                if c_u in final_pos_code and c_v in final_pos_code:
                    cp1 = final_pos_code[c_u];
                    cp2 = final_pos_code[c_v]
                    _, (ccx, ccy) = GraphVisualizer.create_bezier_path(cp1, cp2, EDGE_RAD)
                    end_x = 0.25 * cp1[0] + 0.5 * ccx + 0.25 * cp2[0]
                    end_y = 0.25 * cp1[1] + 0.5 * ccy + 0.25 * cp2[1]
                    ax.plot(end_x, end_y, 'o', color='#444', markersize=6, zorder=25)
                    valid_target = True

            if valid_target:
                GraphVisualizer.draw_bezier_edge(ax, (start_x, start_y), (end_x, end_y), -0.1, color='black', alpha=0.6,
                                                 width=2.5, linestyle='dashed')

        for node in G_demand.nodes():
            if node in final_pos_demand:
                x, y = final_pos_demand[node]
                ax.text(x, y, node, ha='center', va='center', fontsize=11, fontweight='bold', color='black',
                        bbox=dict(boxstyle="round4,pad=0.6", fc="#FFFFFF", ec="black", lw=2.0, alpha=1.0), zorder=30)

        for node in G_code.nodes():
            if node in final_pos_code:
                x, y = final_pos_code[node]
                ax.text(x, y, node, ha='center', va='center', fontsize=9, fontweight='bold', color='black',
                        bbox=dict(boxstyle="round4,pad=0.6", fc="#E6F3FF", ec="black", lw=2.0, alpha=1.0), zorder=30)

        all_xs = [c[0] for c in final_pos_demand.values()] + [c[0] for c in final_pos_code.values()]
        all_ys = [c[1] for c in final_pos_demand.values()] + [c[1] for c in final_pos_code.values()]

        if all_xs and all_ys:
            margin = 5.0
            ax.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
            ax.set_ylim(min(all_ys) - margin, max(all_ys) + margin)
        else:
            ax.set_xlim(-10, 10);
            ax.set_ylim(-10, 10)

        edge_count = G_demand.number_of_edges() + G_code.number_of_edges()
        ax.set_title(
            f'因果追溯图 (Traceability Graph)\n需求节点(上): {len(G_demand.nodes())} | 代码节点(下): {len(G_code.nodes())} | 总连线: {edge_count}',
            fontsize=20, fontweight='bold', pad=25)

        legend_elements = [
            mpatches.Patch(facecolor='#FFFFFF', edgecolor='black', linewidth=2.0, label='需求节点'),
            mpatches.Patch(facecolor='#E6F3FF', edgecolor='black', linewidth=2.0, label='代码节点'),
            Line2D([0], [0], color='#444', marker='o', markersize=6, linestyle='None', label='边锚点(Edge Anchor)'),
            Line2D([0], [0], color='black', linewidth=2.5, linestyle='dashed', alpha=0.6, label='追溯映射(虚线)'),
            Line2D([0], [0], color='black', linewidth=1.5, linestyle='solid', alpha=0.5, label='内部依赖(实线)')
        ]
        ax.legend(handles=legend_elements, loc='upper right', fontsize=14)
        ax.axis('off')

        img_path = os.path.join(CONFIG['vis_output_dir'], f"{filename_suffix}.png")
        try:
            plt.tight_layout()
            fig.savefig(img_path, dpi=300, bbox_inches='tight', facecolor='white')
            print(f"💾 [保存] 最终因果追溯图: {img_path}")
        except Exception as e:
            print(f"❌ 绘图保存失败: {e}")
        plt.close(fig)

    @staticmethod
    def save_error_trace_graph(G_demand, G_code, trace_connections, error_node, error_edge, filename_suffix):
        print(f"🎨 正在绘制测试错误图...")
        pos_demand_local, _ = GraphVisualizer.tree_layout_topological(G_demand)
        pos_code_local, _ = GraphVisualizer.tree_layout_topological(G_code)

        final_pos_demand = {}
        min_y_demand = 0
        if pos_demand_local:
            min_y_demand = min(y for x, y in pos_demand_local.values())
            for node, (x, y) in pos_demand_local.items(): final_pos_demand[node] = (x, y)

        final_pos_code = {}
        max_y_code = 0
        if pos_code_local: max_y_code = max(y for x, y in pos_code_local.values())

        SECTION_GAP = 25.0
        y_shift = min_y_demand - max_y_code - SECTION_GAP

        for node, (x, y) in pos_code_local.items(): final_pos_code[node] = (x, y + y_shift)

        fig, ax = plt.subplots(figsize=(32, 26))
        EDGE_RAD = 0.2

        for u, v in G_code.edges():
            if u in final_pos_code and v in final_pos_code:
                is_error_edge = error_edge and u == error_edge[0] and v == error_edge[1]
                color = 'red' if is_error_edge else 'black'
                width = 3.0 if is_error_edge else 1.5
                alpha = 1.0 if is_error_edge else 0.5
                GraphVisualizer.draw_bezier_edge(ax, final_pos_code[u], final_pos_code[v], EDGE_RAD, color=color,
                                                 alpha=alpha, width=width)

        for u, v in G_demand.edges():
            if u in final_pos_demand and v in final_pos_demand:
                GraphVisualizer.draw_bezier_edge(ax, final_pos_demand[u], final_pos_demand[v], EDGE_RAD, color='black',
                                                 alpha=0.5, width=1.5)

        for (d_u, d_v), t_type, t_val in trace_connections:
            if d_u not in final_pos_demand or d_v not in final_pos_demand: continue
            p1 = final_pos_demand[d_u];
            p2 = final_pos_demand[d_v]
            _, (cx, cy) = GraphVisualizer.create_bezier_path(p1, p2, EDGE_RAD)
            start_x = 0.25 * p1[0] + 0.5 * cx + 0.25 * p2[0]
            start_y = 0.25 * p1[1] + 0.5 * cy + 0.25 * p2[1]
            ax.plot(start_x, start_y, 'o', color='#444', markersize=6, zorder=25)

            end_x, end_y = 0, 0;
            valid_target = False
            if t_type == 'NODE':
                c_node = t_val
                if c_node in final_pos_code:
                    end_x, end_y = final_pos_code[c_node];
                    valid_target = True
            elif t_type == 'EDGE':
                c_u, c_v = t_val
                if c_u in final_pos_code and c_v in final_pos_code:
                    cp1 = final_pos_code[c_u];
                    cp2 = final_pos_code[c_v]
                    _, (ccx, ccy) = GraphVisualizer.create_bezier_path(cp1, cp2, EDGE_RAD)
                    end_x = 0.25 * cp1[0] + 0.5 * ccx + 0.25 * cp2[0]
                    end_y = 0.25 * cp1[1] + 0.5 * ccy + 0.25 * cp2[1]
                    ax.plot(end_x, end_y, 'o', color='#444', markersize=6, zorder=25)
                    valid_target = True

            if valid_target:
                GraphVisualizer.draw_bezier_edge(ax, (start_x, start_y), (end_x, end_y), -0.1, color='black', alpha=0.6,
                                                 width=2.5, linestyle='dashed')

        for node in G_demand.nodes():
            if node in final_pos_demand:
                x, y = final_pos_demand[node]
                ax.text(x, y, node, ha='center', va='center', fontsize=11, fontweight='bold', color='black',
                        bbox=dict(boxstyle="round4,pad=0.6", fc="#FFFFFF", ec="black", lw=2.0, alpha=1.0), zorder=30)

        for node in G_code.nodes():
            if node in final_pos_code:
                x, y = final_pos_code[node]
                is_error_node = error_node and node == error_node
                fc = "#FFCCCC" if is_error_node else "#E6F3FF"
                ec = "red" if is_error_node else "black"
                lw = 3.0 if is_error_node else 2.0
                ax.text(x, y, node, ha='center', va='center', fontsize=9, fontweight='bold', color='black',
                        bbox=dict(boxstyle="round4,pad=0.6", fc=fc, ec=ec, lw=lw, alpha=1.0), zorder=30)

        all_xs = [c[0] for c in final_pos_demand.values()] + [c[0] for c in final_pos_code.values()]
        all_ys = [c[1] for c in final_pos_demand.values()] + [c[1] for c in final_pos_code.values()]

        if all_xs and all_ys:
            margin = 5.0
            ax.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
            ax.set_ylim(min(all_ys) - margin, max(all_ys) + margin)
        else:
            ax.set_xlim(-10, 10);
            ax.set_ylim(-10, 10)

        edge_count = G_demand.number_of_edges() + G_code.number_of_edges()
        ax.set_title(
            f'测试错误图 (Test Error Graph)\n需求节点(上): {len(G_demand.nodes())} | 代码节点(下): {len(G_code.nodes())} | 总连线: {edge_count}',
            fontsize=20, fontweight='bold', pad=25)

        legend_elements = [
            mpatches.Patch(facecolor='#FFFFFF', edgecolor='black', linewidth=2.0, label='需求节点'),
            mpatches.Patch(facecolor='#E6F3FF', edgecolor='black', linewidth=2.0, label='代码节点'),
            mpatches.Patch(facecolor='#FFCCCC', edgecolor='red', linewidth=3.0, label='报错代码节点'),
            Line2D([0], [0], color='red', linewidth=3.0, linestyle='solid', alpha=1.0, label='报错代码边'),
            Line2D([0], [0], color='#444', marker='o', markersize=6, linestyle='None', label='边锚点(Edge Anchor)'),
            Line2D([0], [0], color='black', linewidth=2.5, linestyle='dashed', alpha=0.6, label='追溯映射(虚线)'),
            Line2D([0], [0], color='black', linewidth=1.5, linestyle='solid', alpha=0.5, label='内部依赖(实线)')
        ]
        ax.legend(handles=legend_elements, loc='upper right', fontsize=14)
        ax.axis('off')

        img_path = os.path.join(CONFIG['vis_output_dir'], f"{filename_suffix}.png")
        try:
            plt.tight_layout()
            fig.savefig(img_path, dpi=300, bbox_inches='tight', facecolor='white')
            print(f"💾 [保存] 测试错误图: {img_path}")
        except Exception as e:
            print(f"❌ 绘图保存失败: {e}")
        plt.close(fig)


# ---------------- 1. 大模型调用函数 ----------------
def test2_ask_question(jason_path='./', model_name='gpt-5.2', chat_content=''):
    _alias = {'gpt-5.2': os.environ.get('TRACE_MODEL', 'gpt-6-sol'),
              'gpt-5.4': os.environ.get('TRACE_MODEL', 'gpt-6-sol'),
              'gpt-5.5': os.environ.get('TRACE_MODEL', 'gpt-6-sol')}
    model_name = _alias.get(model_name, model_name)
    loaded_messages = [
        {"role": "system", "content": "你将扮演一名智能助手回答用户的各种问题。"},
        {"role": "user", "content": chat_content}
    ]
    client = OpenAI(api_key=os.environ.get("TRACE_API_KEY", ""))
    client.base_url = os.environ.get("TRACE_BASE_URL", "https://xlabapi.aitoken.credit/v1")

    _attempts = int(os.environ.get('TRACE_ASK_RETRIES', '4'))
    for _i in range(_attempts):
        try:
            completion = client.chat.completions.create(model=model_name, messages=loaded_messages)
            new_reply = completion.choices[0].message.content or ""
            if new_reply.strip():
                return [new_reply, completion.choices]
            print(f"⚠️ [SMOKE] 第 {_i + 1}/{_attempts} 次调用返回空内容，重试…")
        except Exception as e:
            print(f"大模型调用失败({_i + 1}/{_attempts}): {e}")
        time.sleep(3 + 4 * _i)
    return ["", None]


# ---------------- 测试错误分析函数 ----------------
def analyze_error_and_highlight_graph(demand_graph, code_graph, trace_connections, error_log, filename_suffix):
    code_nodes = list(code_graph.nodes())
    code_edges = list(code_graph.edges())

    prompt = f"""
你是一个高级调试专家。当前代码测试失败，以下是测试报错信息以及因果追溯图的信息。
请根据测试报错，指出因果追溯图中哪个代码节点或代码边最可能导致了该报错。

[测试报错]
{error_log}

[代码节点]
{code_nodes}

[代码边]
{code_edges}

[追溯映射]
{trace_connections}

【强制要求 / CRITICAL INSTRUCTION】
既然测试已经失败，说明图中必定存在未成功实现或存在Bug的节点/边。
你**必须（MUST）**从上述给定的[代码节点]或[代码边]中，挑选出**最相关的一个**进行标注！绝对不允许不返回任何标注！
即使报错信息不够明确，你也必须基于逻辑推断给出一个最有可能的节点或边。

请只返回一个最相关的代码节点或代码边。
如果是节点，请严格按照格式输出：ERROR_NODE: 节点名
如果是边，请严格按照格式输出：ERROR_EDGE: 节点A -> 节点B
不要输出其他任何解释性文字。
"""
    reply, _ = test2_ask_question(chat_content=prompt)

    error_node = None
    error_edge = None

    for line in reply.split('\n'):
        line = line.strip()
        if line.startswith("ERROR_NODE:"):
            error_node = line.replace("ERROR_NODE:", "").strip()
        elif line.startswith("ERROR_EDGE:"):
            edge_str = line.replace("ERROR_EDGE:", "").strip()
            if "->" in edge_str:
                u, v = edge_str.split("->")
                error_edge = (u.strip(), v.strip())

    GraphVisualizer.save_error_trace_graph(demand_graph, code_graph, trace_connections, error_node, error_edge,
                                           filename_suffix)
    return reply


# ---------------- 2. 历史记录管理器 ----------------
class HistoryManager:
    def __init__(self, history_file="task_history_storage.json"):
        self.history_file = history_file
        self.history = self._load_history()

    def _load_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def get_task_history(self, task_id):
        return self.history.get(task_id)

    def save_task_history(self, task_id, prompt, demand_edges, generated_code, code_edges):
        self.history[task_id] = {
            "prompt": prompt,
            "demand_graph_edges": demand_edges,
            "generated_code": generated_code,
            "code_graph_edges": code_edges,
            "timestamp": time.time()
        }
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ 保存历史记录失败: {e}")


# ---------------- 仓库访问管理器 ----------------
import stat


def get_file_skeleton(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        tree = ast.parse(content)
        classes = []
        functions = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, ast.FunctionDef):
                functions.append(node.name)
        classes = [c for c in classes if not c.startswith('_')]
        functions = [f for f in functions if not f.startswith('_')]
        if not classes and not functions:
            return None
        return f"Classes: {classes}, Functions: {functions}"
    except Exception:
        return None


def on_rm_error(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class RepoAccessManager:
    def __init__(self, local_base_dir):
        self.repos_dir = local_base_dir
        if not os.path.exists(self.repos_dir):
            print(f"⚠️ 警告: 本地仓库根目录不存在: {self.repos_dir}")

    def _run_git(self, args, cwd=None, env=None):
        try:
            if env is None: env = os.environ.copy()
            env['GIT_TERMINAL_PROMPT'] = '0'
            result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env,
                                    encoding='utf-8', errors='replace')
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr

    def prepare_repository(self, repo_name, commit_id):
        short_name = repo_name.split('/')[-1]
        full_name_flattened = repo_name.replace('/', '__')
        path_short = os.path.join(self.repos_dir, short_name)
        path_full = os.path.join(self.repos_dir, full_name_flattened)
        target_path = None

        print(f"\n📦 [RepoAccess] 正在本地查找仓库: {repo_name}")
        if os.path.exists(path_short):
            target_path = path_short
        elif os.path.exists(path_full):
            target_path = path_full

        if not target_path:
            print(f"❌ 错误: 在 {self.repos_dir} 下未找到仓库文件夹。")
            return None

        print(f"   ✅ 找到仓库路径: {target_path}")
        print(f"   🎯 目标 Commit: {commit_id}")

        git_dir = os.path.join(target_path, '.git')
        if os.path.exists(git_dir):
            success, current_head = self._run_git(["git", "rev-parse", "HEAD"], cwd=target_path)
            if success and current_head.strip() == commit_id:
                print("   ✅ 当前已经是目标 Commit，无需切换。")
                return target_path

            print(f"   ⚡ 检测到 Git 仓库，正在切换版本 (这将重置该目录的修改)...")
            self._run_git(["git", "reset", "--hard"], cwd=target_path)
            self._run_git(["git", "clean", "-fd"], cwd=target_path)
            success, err_msg = self._run_git(["git", "checkout", "-f", commit_id], cwd=target_path)

            if success:
                print("   ✅ Checkout 成功")
            else:
                print(f"❌ Checkout 失败: {err_msg}")
        else:
            print("   ⚠️ 未检测到 .git 目录，跳过版本切换，直接使用当前文件内容。")

        return target_path

    # 返回值 is_fallback，用来标记是否触发了终极保底
    def identify_target_file(self, issue_description, repo_path):
        print(f"🕵️ [AST增强版] 正在通过代码骨架分析定位目标文件...")
        potential_keywords = set(re.findall(r'\b[A-Z]\w+\b', issue_description))
        potential_keywords.update(re.findall(r'\b\w+\(\)', issue_description))

        keywords = [k.replace('()', '') for k in potential_keywords if len(k) > 3]
        ignore_words = {'Error', 'Bug', 'Issue', 'Python', 'Django', 'Test', 'File', 'Path', 'None', 'True', 'False',
                        'Exception'}
        keywords = [k for k in keywords if k not in ignore_words]

        print(f"🔍 Issue 关键词提取: {keywords}")
        skeleton_list = []
        all_files = []

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            for root_dir, dirs, files_in_dir in os.walk(repo_path):
                if '.git' in root_dir: continue
                for f in files_in_dir:
                    if f.endswith('.py'):
                        full_path = os.path.join(root_dir, f)
                        rel_path = os.path.relpath(full_path, repo_path)
                        all_files.append(rel_path)
                        skeleton = get_file_skeleton(full_path)
                        if skeleton:
                            is_relevant = False
                            if any(kw.lower() in f.lower() for kw in keywords): is_relevant = True
                            if any(kw in skeleton for kw in keywords): is_relevant = True
                            if not keywords and "test" not in f.lower(): is_relevant = True
                            if is_relevant:
                                skeleton_list.append(f"File: {rel_path}\n   {skeleton}")

        context_text = "\n".join(skeleton_list[:100])
        if not context_text:
            print("⚠️ 未提取到有效骨架关联，尝试使用纯文件名列表...")
            context_text = "\n".join(all_files[:200])

        system_prompt = "You are a Principal Software Architect. Locate the EXACT file that needs modification."
        user_prompt = f"""
========== Issue Description ==========
{issue_description}

========== Repository Skeleton (Classes & Functions) ==========
{context_text}

========== Task ==========
Analyze the skeleton context above.
1. Look for the Class or Function mentioned in the Issue.
2. Identify which file defines that Class/Function.
3. Return ONLY the relative file path string. Do not output sentences.
"""
        print(f"📤 发送给 LLM 的条目数: {len(skeleton_list)}")
        try:
            # 【修改】定位文件时调用 gpt-5.4
            reply, _ = test2_ask_question(chat_content=system_prompt + "\n" + user_prompt, model_name='gpt-5.4')
        except Exception as e:
            print(f"❌ LLM 调用失败: {e}")
            return None, None, False

        reply = reply.replace('\\_', '_')
        print(f"🤖 LLM 原始回复: {reply[:100]}...")

        target_file = None
        clean_reply = reply.strip().replace('`', '').replace("'", "").replace('"', "").split('\n')[0].strip()

        if os.path.exists(os.path.join(repo_path, clean_reply)) and clean_reply.endswith('.py'):
            target_file = clean_reply

        potential_paths = []
        if not target_file:
            potential_paths = re.findall(r'[\w\-\./\\]+\.py', reply)
            for path in potential_paths:
                clean_path = path.strip().strip('.').strip(',')
                full_check_path = os.path.join(repo_path, clean_path)
                if os.path.exists(full_check_path):
                    target_file = clean_path
                    print(f"✅ [正则命中] 找到存在的文件: {target_file}")
                    break

        if not target_file and potential_paths:
            print("⚠️ 提供的路径不存在，尝试在仓库中全局搜索文件名...")
            for path in potential_paths:
                clean_path = path.strip().strip('.').strip(',')
                basename = os.path.basename(clean_path.replace('\\', '/'))
                for root_dir, _, files_in_dir in os.walk(repo_path):
                    if basename in files_in_dir:
                        target_file = os.path.relpath(os.path.join(root_dir, basename), repo_path)
                        print(f"✅ [全局搜索命中] 找到存在的文件: {target_file}")
                        break
                if target_file:
                    break

        if target_file:
            full_path = os.path.join(repo_path, target_file)
            print(f"✅ [最终定位] 锁定目标文件: {target_file}")
            return target_file, full_path, False
        else:
            print(f"❌ 模型定位失败，尝试从 Issue 描述中直接正则搜索所有可能的 .py 文件...")
            matches = re.findall(r'([\w/\\]+\.py)', issue_description)

            for backup in matches:
                backup_clean = backup.strip().strip('.').strip(',')
                full_backup_path = os.path.join(repo_path, backup_clean)

                if os.path.exists(full_backup_path):
                    print(f"✅ 使用正则备用文件: {backup_clean}")
                    return backup_clean, full_backup_path, False

                backup_basename = os.path.basename(backup_clean.replace('\\', '/'))
                for root_dir, _, files_in_dir in os.walk(repo_path):
                    if backup_basename in files_in_dir:
                        target_file = os.path.relpath(os.path.join(root_dir, backup_basename), repo_path)
                        print(f"✅ [备用全局搜索命中] 找到存在的文件: {target_file}")
                        return target_file, os.path.join(repo_path, target_file), False

            # 触发终极保底，返回 True 作为第三个返回值
            if skeleton_list:
                fallback_file = skeleton_list[0].split('\n')[0].replace('File: ', '').strip()
                fallback_full = os.path.join(repo_path, fallback_file)
                if os.path.exists(fallback_full):
                    print(f"⚠️ [终极保底] 强制使用骨架列表中的第一个文件: {fallback_file}")
                    return fallback_file, fallback_full, True
            elif all_files:
                fallback_file = all_files[0]
                fallback_full = os.path.join(repo_path, fallback_file)
                if os.path.exists(fallback_full):
                    print(f"⚠️ [终极保底] 强制使用所有文件列表中的第一个文件: {fallback_file}")
                    return fallback_file, fallback_full, True

            print("❌ 所有备用方案均失败，无法定位文件。")
            return None, None, False


# ---------------- 核心分析与处理函数 ----------------
def analyze_requirement_change_impact(old_prompt, old_edges, new_prompt, new_edges, new_code):
    system_prompt = """你是一名高级软件需求分析师和代码审计专家。
你的任务是分析同一个编程任务在需求（Prompt）发生变化后，其需求因果图的变化，并具体指出这种变化如何影响了最终生成的代码。

分析步骤：
1. 对比“旧Prompt”和“新Prompt”，总结需求的核心变更点。
2. 对比“旧需求因果图边”和“新需求因果图边”，找出增加、删除或改变方向的边。
3. 结合“新生成的代码”，具体指出需求因果图的变更导致了代码中哪些具体的逻辑、函数调用或变量处理发生了改变。

输出格式要求：
请输出一份详细的《需求变更影响分析报告》，包含以下章节：
1. **需求变更摘要**：简述需求发生了什么变化。
2. **因果图变更分析**：列出变化的边（例如：新增 A->B，移除 C->D），并解释其语义含义。
3. **代码影响追踪**：这是最重要的部分。请明确指出因果图的变更具体对应到了代码的哪一部分（请引用代码片段或行号逻辑）。
"""
    user_prompt = f"""
========== 旧版本信息 ==========
[旧 Prompt]: {old_prompt}
[旧 需求因果图边列表]: {old_edges}
========== 新版本信息 ==========
[新 Prompt]: {new_prompt}
[新 需求因果图边列表]: {new_edges}
========== 新生成的代码/Patch ==========
{new_code}
========== 任务 ==========
请根据上述信息，生成《需求变更影响分析报告》。
"""
    print("正在调用大模型进行需求变更影响分析...")
    reply, _ = test2_ask_question(chat_content=system_prompt + "\n" + user_prompt)
    return reply


def create_trace_graph_structure_prompt(demand_graph, code_graph, trace_report):
    demand_edges_str = "\n".join([f"{u} -> {v}" for u, v in demand_graph.edges()])
    code_edges_str = "\n".join([f"{u} -> {v}" for u, v in code_graph.edges()])
    code_nodes_str = ", ".join(list(code_graph.nodes()))

    return f"""
    你是一个专业的数据可视化架构师。
    你的任务是根据“需求因果图”、“代码因果图”和“因果追溯报告”，构建    一个精确的“因果追溯图”结构数据。

    ========== 输入数据 ==========
    [需求因果图边]:
    {demand_edges_str}

    [代码因果图节点]: {code_nodes_str}
    [代码因果图边]:
    {code_edges_str}

    [因果追溯报告]:
    {trace_report}

    ========== 输出要求 ==========
    请输出一个结构化的列表，明确指出每一条需求边（Demand Edge）对应到了代码中的哪个元素（代码节点或代码边）。

    请严格使用以下格式（MAPPING: 需求边 >>> 代码元素）：

    格式 1 (对应代码边):
    MAPPING: DemandNodeA -> DemandNodeB >>> CodeFuncA -> CodeFuncB

    格式 2 (对应代码节点):
    MAPPING: DemandNodeA -> DemandNodeB >>> CodeFuncNode

    注意：
    1. 请确保节点名称与输入数据完全一致。
    2. 不要输出任何解释性文字，只输出 MAPPING 行。
    3. 如果有多个代码实现，请选择最核心的一个。
    """


def parse_trace_graph_structure(response_text):
    connections = []
    lines = response_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line.startswith("MAPPING:"): continue
        content = line.replace("MAPPING:", "").strip()
        if ">>>" not in content: continue
        parts = content.split(">>>")
        if len(parts) != 2: continue

        left_part = parts[0].strip()
        right_part = parts[1].strip()

        left_part = re.sub(r'DEMAND_EDGE\((.*?)\)', r'\1', left_part)
        right_part = re.sub(r'CODE_EDGE\((.*?)\)', r'\1', right_part)
        right_part = re.sub(r'CODE_NODE\((.*?)\)', r'\1', right_part)

        if "->" not in left_part: continue
        d_u, d_v = [x.strip() for x in left_part.split("->", 1)]

        if "->" in right_part:
            c_raw_u, c_raw_v = right_part.split("->", 1)

            def clean_node(n):
                return re.sub(r'\(.*?\)', '', n).replace('（', '').replace('）', '').strip()

            c_u = clean_node(c_raw_u);
            c_v = clean_node(c_raw_v)
            connections.append(((d_u, d_v), 'EDGE', (c_u, c_v)))
        else:
            c_node = re.sub(r'\(.*?\)', '', right_part).replace('（', '').replace('）', '').strip()
            connections.append(((d_u, d_v), 'NODE', c_node))
    return connections


def generate_initial_graph_via_llm(prompt_text):
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
    user_prompt = f"========== 编程题目描述 ==========\n{prompt_text}\n\n========== 任务 ==========\n请根据上述题目，模拟 GES 算法和 BIC 打分逻辑，构建初始因果图。\n请列出所有的节点（关键词）和有向边。"
    print("正在调用大模型模拟 GES 算法构建初始因果图...")
    reply, _ = test2_ask_question(chat_content=system_prompt + "\n" + user_prompt)
    return reply


def parse_llm_graph_to_networkx(reply_text, current_tokens):
    G = nx.DiGraph()
    lines = reply_text.strip().split('\n')
    all_nodes = set()
    for line in lines:
        match = re.search(r'([a-zA-Z0-9_]+)\s*(?:->|→|=>)\s*([a-zA-Z0-9_]+)', line)
        if match:
            u, v = match.group(1), match.group(2)
            all_nodes.add(u);
            all_nodes.add(v)
            G.add_edge(u, v)
    if len(G.nodes()) == 0:
        from collections import Counter
        top_words = [w for w, _ in Counter(current_tokens).most_common(5)]
        for i in range(len(top_words) - 1): G.add_edge(top_words[i], top_words[i + 1])
        all_nodes = set(top_words)
    return G, list(all_nodes)


def format_graph_info_for_llm(graph, labels, prompt_text, problem_info):
    edges_list = [f"{i} → {j}" for i, j in graph.edges()]
    nodes_info = "\n".join([f"- {node}" for node in labels])
    system_prompt = """你是一名因果图优化专家，需要根据编程题目的内容和语义来优化因果图。
分析要求：
1. 仔细阅读题目描述，理解其逻辑和要求
2. 分析图中的节点（关键词）之间的真实因果关系
3. 基于题目语义，判断边是否存在真实的因果或依赖关系
4. 删除伪相关、冗余或不符合题目逻辑的边
5. 可以添加题目中隐含但图中缺失的重要因果关系
6. 确保优化后的图能更好地反映题目要求的逻辑关系

输出格式要求：
请输出优化后的边列表，每行一条边，格式为：源节点 → 目标节点
如果认为应该删除所有边，可以输出"无相关边"。
"""
    user_prompt = f"========== 题目信息 ==========\n{problem_info}\n\n========== 初始因果图信息 (基于 GES 逻辑生成) ==========\n节点列表：\n{nodes_info}\n\n原始有向边：\n{'; '.join(edges_list) if edges_list else '暂无边'}\n\n========== 分析任务 ==========\n请基于以上题目内容和语义，优化这个因果图。\n请给出优化后的边列表："
    return system_prompt, user_prompt


def parse_llm_reply_for_edges(reply_text):
    optimized_edges = []
    for line in reply_text.strip().split('\n'):
        match = re.search(r'(\w+)\s*[→]\s*(\w+)', line)
        if not match: match = re.search(r'(\w+)\s*->\s*(\w+)', line)
        if match: optimized_edges.append((match.group(1), match.group(2)))
    return optimized_edges


def normalize_patch(patch_content: str) -> str:
    if not patch_content: return ""
    lines = patch_content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    fixed_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(('diff --git', '--- ', '+++ ')):
            line = line.replace('\\', '/')
        if line.startswith('--- ') and '/dev/null' not in line:
            if not line.startswith('--- a/'):
                clean_path = line[4:].strip().lstrip('/').lstrip('\\').lstrip('a/').lstrip('a\\')
                line = f"--- a/{clean_path}"
        elif line.startswith('+++ ') and '/dev/null' not in line:
            if not line.startswith('+++ b/'):
                clean_path = line[4:].strip().lstrip('/').lstrip('\\').lstrip('b/').lstrip('b\\')
                line = f"+++ b/{clean_path}"
        if line.startswith('@@'):
            match = re.match(r'^@@\s-(\d+)(?:,(\d+))?\s\+(\d+)(?:,(\d+))?\s@@(.*)$', line)
            if match:
                start_old, start_new = match.group(1), match.group(3)
                suffix = match.group(5)
                j = i + 1
                count_old, count_new = 0, 0
                hunk_lines = []
                while j < len(lines):
                    nl = lines[j]
                    if nl.startswith(('diff --git', '@@', '--- ')): break
                    if not nl.startswith(('-', '+', '\\')):
                        if nl == '':
                            nl = ' '
                        elif not nl.startswith(' '):
                            nl = ' ' + nl
                    hunk_lines.append(nl)
                    if nl.startswith('-'):
                        count_old += 1
                    elif nl.startswith('+'):
                        count_new += 1
                    elif nl.startswith(' '):
                        count_old += 1;
                        count_new += 1
                    j += 1
                fixed_lines.append(f"@@ -{start_old},{count_old} +{start_new},{count_new} @@{suffix}")
                fixed_lines.extend(hunk_lines)
                i = j
                continue
        fixed_lines.append(line)
        i += 1
    return '\n'.join(fixed_lines).strip() + '\n'


def extract_and_clean_patch(response):
    patch_match = re.search(r'```(?:diff)?(.*?)```', response, re.DOTALL)
    if patch_match:
        raw_patch = patch_match.group(1)
    else:
        if "diff --git" in response:
            raw_patch = response[response.find("diff --git"):]
        else:
            raw_patch = response
    text = raw_patch.strip()
    explanation = ""
    expl_match = re.search(r'=== BEGIN EXPLANATION ===(.*?)=== END EXPLANATION ===', response, re.DOTALL)
    if expl_match: explanation = expl_match.group(1).strip()
    return normalize_patch(text), explanation


import difflib


def generate_diff_locally(original_content, new_content, file_path):
    file_path = file_path.replace('\\', '/')
    original_lines = original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines, new_lines,
        fromfile=f"a/{file_path}", tofile=f"b/{file_path}", lineterm='\n'
    )
    diff_text = "".join(diff)
    if not diff_text: return None
    header = f"diff --git a/{file_path} b/{file_path}\n--- a/{file_path}\n+++ b/{file_path}\n"
    diff_lines = diff_text.splitlines(keepends=True)
    return header + "".join(diff_lines[2:]) if len(diff_lines) >= 2 else header + diff_text


def extract_code_content(response_text):
    match = re.search(r'```(?:\w+)?\s*(.*?)```', response_text, re.DOTALL)
    if match: return match.group(1).strip()
    lines = response_text.split('\n')
    return '\n'.join([l for l in lines if not l.strip().startswith(("Here is", "Explanation"))]).strip()


CODE_BLOCK_START = "```python"
CODE_BLOCK_END = "```"


def build_full_file_prompt(rel_path, issue_desc, content, graph_info):
    return f"""You are a Principal Software Engineer.
The file `{rel_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_path}">
{content}
</file_content>

<auxiliary_info>
Demand Causal Graph: {json.dumps(graph_info)}
</auxiliary_info>

## CRITICAL INSTRUCTION
Please rewrite the **ENTIRE FILE** content of `{rel_path}` to fix the issue.
1. Output the FULL content of the file.
2. DO NOT omit any lines. DO NOT use `...` or `// rest of code`.
3. Wrap the content in a code block like:
{CODE_BLOCK_START}
... content ...
{CODE_BLOCK_END}
"""


def build_diff_prompt(rel_path, issue_desc, content, graph_info):
    return f"""You are a Principal Software Engineer.
The file `{rel_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_path}">
{content}
</file_content>

<auxiliary_info>
Demand Causal Graph: {json.dumps(graph_info)}
</auxiliary_info>

## CRITICAL INSTRUCTION
The file may be large. Please fix the issue by providing a **Unified Diff**.
1. ONLY output the Unified Diff patch.
2. DO NOT output the entire file.
3. Wrap the diff in a ```diff ... ``` block.
4. Ensure the diff header matches the file path exactly:
--- a/{rel_path}
+++ b/{rel_path}
"""


def create_graph_only_prompt(prompt_text, generated_patch):
    return f"""
Generate a "Code Causal Graph" based on this PATCH.

<patch>
{generated_patch}
</patch>

Format:
=== BEGIN CODE CAUSAL GRAPH ===
NodeA -> NodeB [calls]
...
=== END CODE CAUSAL GRAPH ===
"""


def extract_code_graph(response):
    graph_match = re.search(r'=== BEGIN CODE CAUSAL GRAPH ===(.*?)=== END CODE CAUSAL GRAPH ===', response, re.DOTALL)
    if graph_match:
        return graph_match.group(1).strip()
    lines = response.strip().split('\n')
    valid_lines = [l for l in lines if '->' in l or '→' in l]
    if len(valid_lines) > 0:
        return "\n".join(valid_lines)
    return ""


def build_code_causal_graph_from_text(graph_text):
    G = nx.DiGraph()
    if not graph_text: return G
    for line in graph_text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'): continue
        match = re.search(r'(\w+)\s*(?:->|→|=>)\s*(\w+)\s*(?:\[(.*?)\])?', line)
        if match:
            u, v, rel = match.groups()
            rel = rel if rel else "depends_on"
            G.add_edge(u, v, relation=rel)
            G.nodes[u]['type'] = 'code_element'
            G.nodes[v]['type'] = 'code_element'
    return G


# ================= [修改] 大模型自我编译检查机制 =================
def generate_file_with_llm_self_check(
        rel_file_path: str,
        issue_desc: str,
        file_content: str,
        demand_graph_info: Dict[str, Any],
        model_name: str = "gpt-5.4",  # 【修改】默认使用 gpt-5.4
        max_attempts: int = 3,
) -> Tuple[str, str, str]:
    """
    让大模型生成“完整文件内容”，然后让大模型自己进行自我编译检查。
    如果大模型认为有语法错误，则让其重新生成，直到其输出 COMPILE_SUCCESS 或达到上限。
    """
    base_prompt = build_full_file_prompt(rel_file_path, issue_desc, file_content, demand_graph_info)

    print(f"🤖 正在生成初始代码 (尝试 1/{max_attempts})...")
    new_file_response, _ = test2_ask_question(
        chat_content=base_prompt,
        model_name=model_name
    )
    current_code = extract_code_content(new_file_response)
    last_raw = new_file_response or ""

    if not current_code or not current_code.strip():
        return "", "Empty content returned by model.", last_raw

    for attempt in range(2, max_attempts + 1):
        print(f"🔍 正在让大模型进行自我编译检查 (第 {attempt - 1} 次检查)...")
        check_prompt = f"""
You are a strict Python compiler and syntax checker.
Please review the following Python code for any syntax errors, indentation errors, or structural issues that would cause a compilation failure (e.g., `SyntaxError`, `IndentationError`).

<code_to_check>
{current_code}
</code_to_check>

If the code is perfectly valid Python and will compile without syntax errors, output EXACTLY and ONLY:
COMPILE_SUCCESS

If there are syntax errors, explain them briefly and then output the ENTIRE corrected file content wrapped in a ```python code block.
DO NOT omit any lines. DO NOT use `...` or `// rest of code`.
"""
        check_response, _ = test2_ask_question(
            chat_content=check_prompt,
            model_name=model_name
        )

        if check_response and "COMPILE_SUCCESS" in check_response and "```python" not in check_response:
            print(f"✅ [检查通过] 大模型自我检查未发现语法错误！")
            return current_code, "", check_response

        print(f"⚠️ [检查未通过] 大模型发现了潜在错误，正在提取修复后的代码...")
        fixed_code = extract_code_content(check_response)
        if fixed_code and fixed_code.strip():
            current_code = fixed_code
            last_raw = check_response
        else:
            print("⚠️ 未能从修复回复中提取到完整代码，将保留上一版本。")

    return current_code, "Reached max attempts for LLM self-check.", last_raw


def generate_patch_with_llm(
        rel_file_path: str,
        issue_desc: str,
        file_content: str,
        demand_graph_info: Dict[str, Any],
        model_name: str = "gpt-5.4",  # 【修改】默认使用 gpt-5.4
) -> Tuple[str, str, str]:
    """
    让大模型直接生成 Unified Diff 补丁，解决大文件超限问题。
    """
    prompt = build_diff_prompt(rel_file_path, issue_desc, file_content, demand_graph_info)

    print(f"🤖 正在要求大模型生成 Diff 补丁...")
    response, _ = test2_ask_question(
        chat_content=prompt,
        model_name=model_name
    )

    if not response or not response.strip():
        return "", "Empty response returned by model.", response or ""

    # 使用原代码中已有的提取函数
    patch, _ = extract_and_clean_patch(response)

    if not patch or not patch.strip():
        return "", "Failed to extract diff patch from response.", response

    return patch, "", response


# ================= [新增] 基于测试报错重试生成 Patch =================
def generate_fixed_patch_with_llm(
        rel_file_path: str,
        issue_desc: str,
        file_content: str,
        previous_patch: str,
        error_log: str,
        error_analysis: str,
        model_name: str = "gpt-5.4",  # 【修改】默认使用 gpt-5.4
) -> Tuple[str, str, str]:
    """
    当测试失败时，将报错信息、错误图分析结果和之前的Patch发给大模型，让其重新生成修复后的 Diff。
    """
    prompt = f"""You are a Principal Software Engineer.
The file `{rel_file_path}` has an issue described below.

<issue>
{issue_desc}
</issue>

<file_content path="{rel_file_path}">
{file_content}
</file_content>

You previously generated this patch, but it failed the tests:
<previous_patch>
{previous_patch}
</previous_patch>

The test execution returned the following error log:
<test_error_log>
{error_log}
</test_error_log>

Based on our Causal Traceability Graph analysis, the error is likely caused by the following code element:
<error_graph_analysis>
{error_analysis}
</error_graph_analysis>

## CRITICAL INSTRUCTION
Please fix the issue and the test failures by providing a new **Unified Diff**.
1. ONLY output the Unified Diff patch.
2. DO NOT output the entire file.
3. Wrap the diff in a ```diff ... ``` block.
4. Ensure the diff header matches the file path exactly:
--- a/{rel_file_path}
+++ b/{rel_file_path}
"""
    print(f"🤖 正在要求大模型根据报错信息修复并生成新的 Diff 补丁...")
    response, _ = test2_ask_question(
        chat_content=prompt,
        model_name=model_name
    )

    if not response or not response.strip():
        return "", "Empty response returned by model.", response or ""

    patch, _ = extract_and_clean_patch(response)

    if not patch or not patch.strip():
        return "", "Failed to extract diff patch from response.", response

    return patch, "", response


# ---------------- [修改] 单题测试执行函数 ----------------
def evaluate_prediction_swebench(instance_id: str, model_patch: str, output_dir: str) -> Dict[str, Any]:
    """[SMOKE] Docker/SWE-bench evaluation is stubbed: it only writes the prediction file."""
    print(f"\n🧪 [SMOKE] 跳过官方 SWE-bench 评测（无 Docker），仅写出预测文件: {instance_id}")
    try:
        os.makedirs(output_dir, exist_ok=True)
        pred = os.path.join(output_dir, f"pred_{instance_id.replace('/', '_')}.json")
        with open(pred, "w", encoding="utf-8", newline='\n') as f:
            json.dump([{"instance_id": instance_id, "model_patch": model_patch or "",
                        "model_name_or_path": "smoke"}], f, ensure_ascii=False, indent=2)
        print(f"💾 预测文件已写入: {pred}")
    except Exception as e:
        print(f"❌ 写预测文件失败: {e}")
    return {"status": "unresolved", "error_log": "SMOKE: evaluation stubbed (no Docker daemon)."}


def _unused_original_evaluate_prediction_swebench(instance_id: str, model_patch: str, output_dir: str) -> Dict[str, Any]:
    print(f"\n🚀 准备进行本地 SWE-bench 评估: {instance_id}")

    if model_patch:
        model_patch = model_patch.replace('\r\n', '\n').replace('\r', '\n')
        if not model_patch.endswith('\n'):
            model_patch += '\n'

    abs_output_dir = os.path.abspath(output_dir)
    pred_filename = os.path.join(abs_output_dir, f"pred_{instance_id.replace('/', '_')}.json")

    prediction_data = [{
        "instance_id": instance_id,
        "model_patch": model_patch,
        "patch": model_patch,
        "model_name_or_path": "causal-agent-local",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }]

    try:
        with open(pred_filename, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(prediction_data, f, ensure_ascii=False, indent=2)
        print(f"💾 预测文件已写入: {pred_filename}")
    except Exception as e:
        print(f"❌ 写入预测文件失败: {e}")
        return {"status": "error"}

    wrapper_content = r"""
import sys, runpy, os, builtins, functools, io, time

# Windows Encoding Fix
if sys.platform.startswith('win'):
    def _hook(orig):
        @functools.wraps(orig)
        def h(*a, **k):
            al = list(a)
            m = al[1] if len(al)>1 else k.get('mode','r')
            if ('w' in m or 'a' in m) and 'b' not in m:
                if len(al)>5: al[5]='\n'
                else: k['newline']='\n'
                if len(al)>3: 
                    if al[3] is None: al[3]='utf-8'
                elif 'encoding' not in k: k['encoding']='utf-8'
            return orig(*tuple(al), **k)
        return h
    builtins.open = _hook(builtins.open); io.open = _hook(io.open)
    sys.modules['resource'] = type('M',(),{'__getattr__':lambda s,n:lambda *a,**k:None})()
    sys.modules['fcntl'] = type('M',(),{'__getattr__':lambda s,n:lambda *a,**k:None})()

sys.stdout.reconfigure(encoding='utf-8')

# Docker Mirror Patch
try:
    import docker.models.images
    original_pull = docker.models.images.ImageCollection.pull
    def patched_pull(self, repository, tag=None, **kwargs):
        mirrors = ["docker.m.daocloud.io", "docker.1panel.live", "hub.rat.dev"]
        repo_name = repository.replace("docker.io/", "") if "docker.io/" in repository else repository
        for mirror in mirrors:
            try:
                print(f"[Docker Patch] 尝试镜像: {mirror}/{repo_name}:{tag}")
                image = original_pull(self, f"{mirror}/{repo_name}", tag, **kwargs)
                image.tag(repository, tag=tag)
                return image
            except: continue
        return original_pull(self, repository, tag, **kwargs)
    docker.models.images.ImageCollection.pull = patched_pull
except: pass

# Run Evaluation
try: 
    print("🚀 启动 SWE-bench 评估主程序...")
    runpy.run_module('swebench.harness.run_evaluation', run_name='__main__', alter_sys=True)
except: 
    import traceback; traceback.print_exc()
"""
    wrapper_path = os.path.join(abs_output_dir, "run_eval_wrapper.py")
    with open(wrapper_path, "w", encoding="utf-8", newline='\n') as f:
        f.write(wrapper_content)

    run_id = f"eval_run_{int(time.time())}"
    cmd = [sys.executable, wrapper_path,
           "--dataset_name", "princeton-nlp/SWE-bench_Lite",
           "--predictions_path", pred_filename,
           "--max_workers", "1",
           "--run_id", run_id,
           "--report_dir", abs_output_dir,
           "--timeout", "1800"]

    print(f"⚙️ 执行评估命令 (RunID: {run_id})...")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, env=env, encoding='utf-8', errors='replace')

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None: break
        if line: print(line.strip())

    print(f"🏁 评估进程结束")

    # ================= [修复] 报告解析逻辑 =================
    print("\n🔍 正在解析评估报告...")
    status = "completed_unknown"

    # 1. 优先查找根目录下的总报告
    root_report_name = f"causal-agent-local.{run_id}.json"
    root_report_path = os.path.join(CURRENT_DIR, root_report_name)

    if os.path.exists(root_report_path):
        print(f"📄 找到总评估报告: {root_report_path}")
        try:
            with open(root_report_path, 'r', encoding='utf-8') as f:
                root_data = json.load(f)

            resolved_ids = root_data.get("resolved_ids", [])
            if instance_id in resolved_ids:
                print(f"✅ 修复成功 (在总报告中标记为 resolved)！")
                status = "resolved"
            else:
                print(f"❌ 修复失败 (未在总报告的 resolved_ids 中找到)")
                status = "unresolved"
        except Exception as e:
            print(f"⚠️ 解析总报告失败: {e}")

    # 2. 查找详细的 report.json 以打印具体用例情况
    found_detailed_report = None
    search_dirs = [
        os.path.join(CURRENT_DIR, "logs", "run_evaluation", run_id),
        abs_output_dir
    ]

    for s_dir in search_dirs:
        if not os.path.exists(s_dir): continue
        for root, dirs, files in os.walk(s_dir):
            if "report.json" in files:
                found_detailed_report = os.path.join(root, "report.json")
                break
        if found_detailed_report: break

    if found_detailed_report:
        print(f"📄 找到详细报告文件: {found_detailed_report}")
        try:
            with open(found_detailed_report, 'r', encoding='utf-8') as f:
                report_data = json.load(f)

            instance_report = report_data.get(instance_id, report_data)
            is_resolved = instance_report.get("resolved", False)

            if is_resolved:
                print(f"✅ 详细报告显示: 修复成功 (resolved: true)")
                if status == "completed_unknown":
                    status = "resolved"
            else:
                print("❌ 详细报告显示: 修复失败 (resolved: false)")
                if status == "completed_unknown":
                    status = "unresolved"
        except Exception as e:
            print(f"⚠️ 解析详细报告失败: {e}")
    else:
        if status == "completed_unknown":
            print("⚠️ 未找到任何 report.json 或总报告。可能是评估过程中途崩溃或 Patch 应用失败。")

    # 提取错误日志供后续分析使用
    error_log = ""
    if status == "unresolved":
        for s_dir in search_dirs:
            if not os.path.exists(s_dir): continue
            for root, dirs, files in os.walk(s_dir):
                if "eval.log" in files:
                    try:
                        with open(os.path.join(root, "eval.log"), 'r', encoding='utf-8', errors='ignore') as f:
                            lines = f.readlines()
                            error_log = "".join(lines[-200:])  # 获取最后200行作为报错信息
                    except Exception as e:
                        print(f"⚠️ 读取 eval.log 失败: {e}")
                    break
            if error_log: break

    return {"status": status, "error_log": error_log}

    # ============================================================================
    # 核心主循环：一题一题进行测试并记录
    # ============================================================================


def main():
    # [SMOKE] 直接读本地 parquet，避免 datasets 依赖
    import pandas as _pd
    _parq = os.environ.get('TRACE_PARQUET', r'D:\comuatedp\trace_run\swe_lite_test.parquet')
    try:
        print(f"正在加载本地 SWE-bench Lite parquet: {_parq}")
        dataset = _pd.read_parquet(_parq).to_dict('records')
    except Exception as e:
        print(f"❌ 加载数据集时出错: {e}")
        return

    # [SMOKE] 只跑一道题
    _want = os.environ.get('TRACE_INSTANCE', '')
    if _want:
        _idx = [i for i in range(len(dataset)) if dataset[i]['instance_id'] == _want]
        if not _idx:
            print(f'❌ 数据集里没有实例 {_want}')
            return
        _order = _idx
    else:
        _order = list(range(min(1, len(dataset))))
    total_tasks = len(_order)

    # 动态维护有效总题数，排除掉触发 fallback_skipped 的题目
    effective_total_tasks = total_tasks

    # 将历史记录文件放到新的输出目录中，彻底与旧数据隔离
    history_file_path = os.path.join(BASE_OUTPUT_DIR, "task_history_storage.json")
    history_manager = HistoryManager(history_file_path)

    passed_count = 0
    evaluated_count = 0

    # 记录失败题目的文件路径
    failed_tasks_file = os.path.join(BASE_OUTPUT_DIR, "failed_tasks.txt")

    def record_failed_task(tid):
        """辅助函数：将失败的题号记录到文件中，避免重复写入"""
        existing_tasks = set()
        if os.path.exists(failed_tasks_file):
            with open(failed_tasks_file, "r", encoding="utf-8") as f:
                existing_tasks = set(line.strip() for line in f if line.strip())

        if tid not in existing_tasks:
            with open(failed_tasks_file, "a", encoding="utf-8") as f:
                f.write(f"{tid}\n")

    for target_index in _order:
        problem = dataset[target_index]
        task_id = problem.get('instance_id', f'SWE-bench/{target_index}')

        print(f"\n{'=' * 80}")
        print(f"🚀 开始处理第 {target_index + 1}/{total_tasks} 题: {task_id}")
        print(f"{'=' * 80}")

        safe_task_id = task_id.replace('/', '_')
        task_output_dir = os.path.join(BASE_OUTPUT_DIR, safe_task_id)
        CONFIG['vis_output_dir'] = task_output_dir

        final_json_path = os.path.join(task_output_dir, f"final_result_{safe_task_id}.json")

        # 保存这题的原始 dataset 条目到单独文件夹
        try:
            dump_path = os.path.join(FULL_DATA_DIR, f"{safe_task_id}.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(problem, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存题目原始数据失败: {e}")

        # ================= 断点续传与单独测试逻辑 =================
        if os.path.exists(final_json_path):
            print(f"⏭️ 题目 {task_id} 已存在最终结果文件，检查测试状态...")
            try:
                with open(final_json_path, 'r', encoding='utf-8') as f:
                    saved_data = json.load(f)

                eval_res = saved_data.get("evaluation_result", {})
                old_eval_status = saved_data.get("evaluation_status", "")

                status = eval_res.get("status", "unknown")
                if not status or status == "unknown" and old_eval_status:
                    status = old_eval_status

                if status in ["pending_batch_eval", "unknown", "completed_unknown"]:
                    print(f"🔄 发现未完成测试的补丁，开始单题测试...")
                    model_patch = saved_data.get("generated_patch", "")
                    if model_patch and model_patch.strip():
                        eval_outcome = evaluate_prediction_swebench(task_id, model_patch, task_output_dir)
                        saved_data["evaluation_result"] = eval_outcome
                        saved_data["evaluation_status"] = eval_outcome.get("status", "unknown")

                        with open(final_json_path, 'w', encoding='utf-8') as f:
                            json.dump(saved_data, f, ensure_ascii=False, indent=2)

                        status = eval_outcome.get("status", "unknown")
                    else:
                        print("⚠️ Patch 为空，跳过测试。")
                        status = "skipped"

                # 如果状态是 fallback_skipped，则在读取历史时直接减少有效题数
                if status == "fallback_skipped":
                    print(f"⏭️ 题目 {task_id} 之前已因触发保底而跳过，不计入总题数。")
                    effective_total_tasks -= 1
                elif status not in ["pending_batch_eval", "unknown", "skipped"]:
                    evaluated_count += 1
                    if status == "resolved":
                        passed_count += 1
                    else:
                        record_failed_task(task_id)

                print(f"📊 当前进度: 已评估 {evaluated_count}/{effective_total_tasks} | 当前通过数: {passed_count}")
                continue
            except Exception as e:
                print(f"⚠️ 读取或更新已保存的结果失败: {e}")

        if not os.path.exists(task_output_dir):
            os.makedirs(task_output_dir)

        try:
            prompt = problem.get("problem_statement", "")
            problem_info = f"Instance ID: {task_id}\nRepo: {problem.get('repo')}\nProblem Statement:\n{prompt}"

            old_task_data = history_manager.get_task_history(task_id)
            is_prompt_changed = False
            old_prompt = None
            old_demand_edges = None

            if old_task_data:
                old_prompt = old_task_data.get("prompt", "")
                if old_prompt.strip() != prompt.strip():
                    print(f"⚠️ 检测到题目 {task_id} 的 Prompt 发生改变！")
                    is_prompt_changed = True
                    old_demand_edges = old_task_data.get("demand_graph_edges", [])
                else:
                    print(f"ℹ️ 题目 {task_id} 的 Prompt 未发生变化。")

            # 1. 需求因果图生成 (默认使用 gpt-5.2)
            try:
                import nltk as _nltk
                _nd = os.environ.get('NLTK_DATA')
                for _pkg in ('punkt', 'punkt_tab', 'stopwords'):
                    try:
                        _nltk.data.find(f'tokenizers/{_pkg}' if _pkg.startswith('punkt') else f'corpora/{_pkg}')
                    except LookupError:
                        _nltk.download(_pkg, download_dir=_nd, quiet=True)
            except Exception as _e:
                print(f'⚠️ [SMOKE] nltk 数据准备告警: {_e}')
            from nltk.tokenize import word_tokenize
            from nltk.corpus import stopwords
            clean = re.sub(r"[^a-zA-Z\s]", " ", prompt)
            tokens = [t for t in word_tokenize(clean.lower()) if t not in stopwords.words('english') and len(t) > 1]

            llm_ges_reply = generate_initial_graph_via_llm(prompt)
            GraphVisualizer.save_text_report(llm_ges_reply, "step1_initial_graph_raw_response")

            G_orig, labels = parse_llm_graph_to_networkx(llm_ges_reply, tokens)
            GraphVisualizer.save_graph_complete(G_orig, "Step 1: Initial Graph (GES)", "step1_initial_graph")

            system_prompt_opt, user_prompt_opt = format_graph_info_for_llm(G_orig, labels, prompt, problem_info)
            reply_opt, _ = test2_ask_question(chat_content=system_prompt_opt + "\n" + user_prompt_opt)
            GraphVisualizer.save_text_report(reply_opt, "step2_demand_graph_raw_response")

            optimized_edges = parse_llm_reply_for_edges(reply_opt)

            demand_causal_graph = nx.DiGraph()
            demand_causal_graph.add_nodes_from(labels)
            for u, v in optimized_edges: demand_causal_graph.add_edge(u, v)
            isolated = [n for n in demand_causal_graph.nodes() if demand_causal_graph.degree(n) == 0]
            demand_causal_graph.remove_nodes_from(isolated)
            if GRAPH_REFINE and _HAS_REFINE:
                demand_causal_graph, _st_i = refine_graph(demand_causal_graph)
                _REFINE_STATS['issue_graph'] = _st_i
                print(f"[B] issue graph refined: {_st_i['edges_before']}->{_st_i['edges_after']} edges, "
                      f"cycles={_st_i['cycles_removed']}, redundant={_st_i['redundant_edges_removed']}")
            GraphVisualizer.save_graph_complete(demand_causal_graph, "Step 2: Demand Causal Graph",
                                                "step2_demand_graph")

            # 2. 代码生成与 Patch 提取
            demand_graph_info = {
                "nodes": list(demand_causal_graph.nodes()),
                "edges": [f"{u}->{v}" for u, v in demand_causal_graph.edges()]
            }

            repo_manager = RepoAccessManager(CONFIG['repo_dir'])
            repo_path = repo_manager.prepare_repository(problem['repo'], problem['base_commit'])
            model_patch = ""
            eval_outcome = {"status": "skipped", "reason": "No patch generated"}

            if repo_path:
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
                        print(f"🛑 [大模型原始回复内容]:\n{llm_raw}\n{'-' * 40}")
                        # 按照你的要求，保留 sys.exit(1) 暂停程序以供排查
                        sys.exit(1)
                    else:
                        # 成功生成 Patch，直接保存
                        GraphVisualizer.save_text_report(model_patch, "final_patch.diff")

            code_causal_graph = nx.DiGraph()
            trace_response = ""
            trace_connections = []

            # 只有在没有被 fallback_skipped 且生成了 patch 的情况下，才进行后续步骤
            if eval_outcome.get("status") != "fallback_skipped":
                if not model_patch or not model_patch.strip():
                    print(f"⚠️ [跳过] 题目 {task_id} 未能成功生成有效 Patch。")
                    # [新增] 打印出为何未生成有效Patch
                    print(f"🛑 [失败原因]: diff 生成失败或补丁为空。")
                    if 'new_file_content' in locals() and new_file_content:
                        print(f"🛑 [附加信息]: 大模型已返回代码，但与原文件对比无差异 (No diff generated)。")
                    # 发现未生成有效 Patch，直接退出程序
                    sys.exit(1)
                else:
                    # 3. 生成代码因果图 (默认使用 gpt-5.2)
                    graph_gen_prompt = create_graph_only_prompt(prompt, model_patch)
                    graph_gen_response, _ = test2_ask_question(chat_content=graph_gen_prompt)
                    code_graph_text = extract_code_graph(graph_gen_response)
                    code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
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
                                                        "step4_code_graph")

                    # 4. 追溯分析 (默认使用 gpt-5.2)
                    trace_analysis_prompt = f"========== Issue 描述 ==========\n{prompt}\n========== 需求因果图 ==========\n{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}\n========== 生成的修复 Patch ==========\n{model_patch}\n========== 生成的代码因果图 ==========\n{code_graph_text}\n{('========== 计算结果（图算法，非模型判断） ==========\n' + computed_block + chr(10)) if computed_block else ''}========== 任务 ==========\n请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。"
                    trace_response, _ = test2_ask_question(chat_content=trace_analysis_prompt)
                    GraphVisualizer.save_text_report(trace_response, "step5_trace_report")

                    struct_prompt = create_trace_graph_structure_prompt(demand_causal_graph, code_causal_graph,
                                                                        trace_response)
                    struct_resp, _ = test2_ask_question(chat_content=struct_prompt)
                    GraphVisualizer.save_text_report(struct_resp, "step5_5_trace_structure_raw")

                    trace_connections = parse_trace_graph_structure(struct_resp)
                    if trace_connections:
                        GraphVisualizer.save_final_trace_graph(demand_causal_graph, code_causal_graph,
                                                               trace_connections, "step5_5_final_trace_graph")

                    # 5. 单题评估
                    eval_outcome = evaluate_prediction_swebench(task_id, model_patch, task_output_dir)

                    # [新增] 测试不通过时的重试机制 (最多 3 次)
                    max_retries = int(os.environ.get('TRACE_MAX_RETRIES', '1'))
                    retry_count = 0

                    while eval_outcome.get("status") == "unresolved" and retry_count < max_retries:
                        retry_count += 1
                        print(f"⚠️ 测试未通过，启动第 {retry_count}/{max_retries} 次重试修复...")

                        error_log = eval_outcome.get("error_log", "未找到详细报错日志")

                        # 生成错误追溯图并获取大模型分析 (默认使用 gpt-5.2)
                        error_analysis = analyze_error_and_highlight_graph(
                            demand_causal_graph,
                            code_causal_graph,
                            trace_connections,
                            (error_log + ("\n\n" + computed_block if computed_block else "")),
                            f"step6_error_trace_graph_retry{retry_count}"
                        )

                        # 【修改】让大模型结合报错信息重新生成代码，明确指定使用 gpt-5.4
                        new_patch, error_msg, llm_raw = generate_fixed_patch_with_llm(
                            rel_file_path=rel_file_path,
                            issue_desc=prompt,
                            file_content=file_content,
                            previous_patch=model_patch,
                            error_log=error_log,
                            error_analysis=(error_analysis + ("\n\n" + computed_block if computed_block else "")),
                            model_name='gpt-5.4'
                        )

                        if not new_patch:
                            print(f"⚠️ 第 {retry_count} 次重试未能生成有效 Patch，跳过本次重试。")
                            break

                        model_patch = new_patch
                        GraphVisualizer.save_text_report(model_patch, f"final_patch_retry{retry_count}.diff")

                        # 重新生成代码因果图和追溯分析 (默认使用 gpt-5.2)
                        graph_gen_prompt = create_graph_only_prompt(prompt, model_patch)
                        graph_gen_response, _ = test2_ask_question(chat_content=graph_gen_prompt)
                        code_graph_text = extract_code_graph(graph_gen_response)
                        code_causal_graph = build_code_causal_graph_from_text(code_graph_text)
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
                                                            f"step4_code_graph_retry{retry_count}")

                        trace_analysis_prompt = f"========== Issue 描述 ==========\n{prompt}\n========== 需求因果图 ==========\n{json.dumps(demand_graph_info, ensure_ascii=False, indent=2)}\n========== 生成的修复 Patch ==========\n{model_patch}\n========== 生成的代码因果图 ==========\n{code_graph_text}\n========== 任务 ==========\n请分析生成的 Patch 和代码因果图是否逻辑上覆盖了需求因果图中的关键依赖关系。"
                        trace_response, _ = test2_ask_question(chat_content=trace_analysis_prompt)
                        GraphVisualizer.save_text_report(trace_response, f"step5_trace_report_retry{retry_count}")

                        struct_prompt = create_trace_graph_structure_prompt(demand_causal_graph, code_causal_graph,
                                                                            trace_response)
                        struct_resp, _ = test2_ask_question(chat_content=struct_prompt)
                        trace_connections = parse_trace_graph_structure(struct_resp)
                        if trace_connections:
                            GraphVisualizer.save_final_trace_graph(demand_causal_graph, code_causal_graph,
                                                                   trace_connections,
                                                                   f"step5_5_final_trace_graph_retry{retry_count}")

                        # 再次评估
                        eval_outcome = evaluate_prediction_swebench(task_id, model_patch, task_output_dir)

                        if eval_outcome.get("status") == "resolved":
                            print(f"✅ 第 {retry_count} 次重试修复成功！")
                            break
                        else:
                            print(f"❌ 第 {retry_count} 次重试修复依然失败。")

            # 统计更新与失败记录
            if eval_outcome.get("status") not in ["skipped", "error", "fallback_skipped"]:
                evaluated_count += 1
                if eval_outcome.get("status") == "resolved":
                    passed_count += 1
                else:
                    record_failed_task(task_id)

            # 6. 保存结果
            final_result = {
                "graph_refine_enabled": bool(GRAPH_REFINE and _HAS_REFINE),
                "graph_refine_stats": _REFINE_STATS,
                "task_id": task_id,
                "generated_patch": model_patch,
                "code_causal_graph": {
                    "nodes": list(code_causal_graph.nodes()),
                    "edges": [{"from": u, "to": v, "relation": d.get('relation')} for u, v, d in
                              code_causal_graph.edges(data=True)]
                },
                "demand_graph": {"nodes": list(demand_causal_graph.nodes()),
                                 "edges": [f"{u}->{v}" for u, v in demand_causal_graph.edges()]},
                "trace_analysis": trace_response,
                "trace_connections": [str(c) for c in trace_connections] if trace_connections else [],
                "evaluation_result": eval_outcome,
                "evaluation_status": eval_outcome.get("status", "unknown")
            }

            if is_prompt_changed and model_patch:
                impact_report = analyze_requirement_change_impact(
                    old_prompt, old_demand_edges, prompt, list(demand_causal_graph.edges()),
                    f"Patch:\n{model_patch}\nGraph:\n{code_graph_text}"
                )
                final_result["impact_analysis"] = impact_report
                GraphVisualizer.save_change_comparison(old_demand_edges, demand_causal_graph, task_id)
                GraphVisualizer.save_text_report(impact_report, "step7_impact_report")

            history_manager.save_task_history(
                task_id, prompt, list(demand_causal_graph.edges()), model_patch, list(code_causal_graph.edges())
            )

            with open(final_json_path, "w", encoding="utf-8") as f:
                json.dump(final_result, f, ensure_ascii=False, indent=2)

            print(f"\n✅ 题目 {task_id} 处理完毕，结果已保存至: {task_output_dir}")
            print(f"📊 当前进度: 已评估 {evaluated_count}/{effective_total_tasks} | 当前通过数: {passed_count}")

        except SystemExit:
            # 捕获 sys.exit() 抛出的 SystemExit 异常，不再向下执行，直接抛出，终止程序
            print(f"\n🛑 检测到严重错误（未生成有效内容或 Patch），程序已主动停止以供排查。")
            raise
        except Exception as e:
            print(f"\n❌ 在处理题目 {task_id} (索引 {target_index}) 时发生错误: {e}")
            traceback.print_exc()
            print(f"🛑 程序已在此题跳过。下次运行将自动重试或继续。")

    # ============================================================================
    # 全部完成后的统计输出
    # ============================================================================
    print("\n" + "=" * 80)
    print("🎉 所有题目测试完毕！")
    print(
        f"📈 总体评估数量: {evaluated_count} / {effective_total_tasks} (原总题数 {total_tasks}，排除了 {total_tasks - effective_total_tasks} 道触发保底的题目)")
    print(f"✅ 成功解决数量 (Resolved): {passed_count}")

    if evaluated_count > 0:
        pass_rate = (passed_count / evaluated_count) * 100
        print(f"🏆 总体通过率: {pass_rate:.2f}%")
    else:
        print("🏆 总体通过率: 0.00%")

    if os.path.exists(failed_tasks_file):
        print(f"📝 未通过的题目已记录至: {failed_tasks_file}")

    print("=" * 80)


if __name__ == "__main__":
    main()

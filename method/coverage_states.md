# Coverage states, witness paths and clue ranking

This note fixes the deterministic part of TRACE-REPAIR: how a requirement relation is judged
against the current candidate, how the judgement is evidenced, and how the resulting clues are
ordered before they are put into a prompt. Everything here is computed by the program; the model
never assigns a coverage state (paper §3.4, §3.7).

Implementation reference: `trace_run/graph_refine.py`
(`issue_coverage`, `rank_clues`, `format_for_prompt`); paper §3.4.

---

## 1. Inputs

| Symbol | Meaning | Where it comes from |
|---|---|---|
| `E_r` | Requirement (= issue) relation edges after extraction, refinement, and removal of nodes that participate in no edge | `prompts/requirement_extraction.md`, `prompts/edge_optimization.md` |
| `G_c^t` | Implementation ("code causal") graph at candidate `t`: nodes and directed edges extracted from the candidate patch | `prompts/implementation_graph.md` |
| `V_c^t`, `E_c^t` | Node set and edge set of `G_c^t` | as above |

Endpoint matching is by **name**, not by semantics: a requirement node covers an implementation
node when the two names are identical. Relation types on implementation edges (`[calls]`,
`[data_flow]`, …) are carried as edge attributes and are **not** consulted by the coverage rules
(paper §3.4). Optional deterministic normalisation (`refine_graph`) may precede the coverage
step: self-loops removed, nodes with degree 0 removed, cycles broken by deleting the weakest edge
(ties broken lexicographically), then transitive reduction. Edge weights only order the removals;
they never enter the coverage rules.

## 2. The four states

Evaluated in this order for every requirement edge `e = (u, v) ∈ E_r`:

| Order | State | Condition | What it asserts |
|---:|---|---|---|
| 1 | `NO_NODE` | `u ∉ V_c^t` or `v ∉ V_c^t` | At least one endpoint does not exist in the implementation graph — the relation has no addressable counterpart in the candidate. |
| 2 | `DIRECT` | `u, v ∈ V_c^t` and `(u, v) ∈ E_c^t` | A direct edge with the same endpoint names and the same direction exists. |
| 3 | `PATH` | `u, v ∈ V_c^t`, `(u, v) ∉ E_c^t`, `v` reachable from `u` | No direct edge, but a multi-hop directed path connects the endpoints. |
| 4 | `UNCOVERED` | `u, v ∈ V_c^t`, `v` not reachable from `u` | Both endpoints exist, yet nothing in the candidate carries the relation. |

Formally:

```
cov_t(e) = NO_NODE    if u ∉ V_c^t or v ∉ V_c^t
cov_t(e) = DIRECT     if u,v ∈ V_c^t and (u,v) ∈ E_c^t
cov_t(e) = PATH       if u,v ∈ V_c^t, (u,v) ∉ E_c^t, and v is reachable from u
cov_t(e) = UNCOVERED  if u,v ∈ V_c^t and v is not reachable from u
```

The order matters: endpoint existence is tested first, so a missing node is never reported as a
missing relation. `NO_NODE` and `UNCOVERED` therefore carry different diagnoses — "the candidate
does not talk about this entity at all" versus "the candidate mentions both entities but never
connects them".

Two boundary behaviours are worth stating explicitly, because they are the ones a reviewer can
check cheaply:

- If implementation-graph extraction returns nothing, `V_c^t = ∅`, and **every** requirement edge
  becomes `NO_NODE`. An extraction failure is therefore visible as a degenerate, all-`NO_NODE`
  state rather than as a silent pass (paper §3.7, "on failure" column).
- `DIRECT`/`PATH` only require name coincidence plus reachability. A requirement relation can
  therefore be structurally covered by code that is behaviourally wrong; structural coverage is
  evidence, not proof. The failed-test signal and the structural signal are kept separate for
  exactly this reason (paper §3.4).

## 3. Witness path for `PATH`

For a `PATH` verdict the pipeline records a shortest witness path and its hop count, so an
indirect realisation can be traced through implementation entities:

```
p_t*(u,v) ∈ arg min_{p ∈ P_t(u,v)} |E(p)|          (P_t = directed paths u → v in G_c^t)
h_t(u,v)  = |E(p_t*(u,v))|
```

Because a direct edge has already been tested, every witness path has at least two hops.
Implementation: `nx.shortest_path` (= BFS on an unweighted digraph), `hops = len(path) - 1`, and
the path itself is stored on the record as `path`. A `PATH` without a stored witness path is not a
valid verdict in this method.

## 4. The uncovered set

```
U_t = { e ∈ E_r | cov_t(e) ∈ { UNCOVERED, NO_NODE } }
```

`U_t` is the set the revision round is conditioned on. It is enumerable, and every member is
backed by the endpoint test or the reachability test above — which is what "computed, not judged"
means operationally. `U_t` is **not** a defect list: an empty `U_t` does not imply the tests pass,
and passing tests do not imply `U_t = ∅`.

In the shipped pipeline the implementation graph, the mapping and the coverage are computed for
the initial candidate and reused during revisions (`G_c^t = G_c^0`, `M_t = M_0`, `cov_t = cov_0`,
`U_t = U_0`; paper §3.6). `U_R` therefore documents the structural state that guided revision; it
is not a re-measurement of structural gaps after revision.

## 5. Clue ranking

Ranking does not change any state and adds no categories. It only decides the order in which the
uncovered relations and candidate implementation elements are presented to the model.

Implemented in `rank_clues(patch_graph, issue_edges, coverage, k=10)`:

1. Only relations whose state is `UNCOVERED` or `NO_NODE` produce clues; `DIRECT` and `PATH`
   produce none.
2. For each such relation `(u, v)`, up to **3** candidate implementation nodes are taken, ordered by
   `(name overlap with {u, v} ↓, PageRank ↓, node name ↑)`. PageRank is `alpha = 0.85`, `tol =
   1e-06`; when the graph has no edges every node gets `1/n`.
3. The clue list is then sorted by
   `(UNCOVERED before NO_NODE, PageRank ↓, name overlap ↓, node name ↑)` and truncated to `k`.
4. Every tie-break ends in a lexicographic comparison, so the output is a deterministic function of
   the two graphs — the same inputs always produce the same clue order.

`NO_NODE` relations are ranked after `UNCOVERED` ones because a missing node names a concrete
element to create or to inspect, while "these two elements are unconnected" says more about the
relation than about a location.

## 6. The rendered block

`format_for_prompt(coverage, clues, stats)` renders the computation into the text that is appended
to the traceability and failure-analysis prompts. Its shape:

```
[Graph refinement] nodes A->B, edges C->D, cycles removed N, redundant edges removed M
[Computed coverage of issue relations] DIRECT=.., PATH=.., NO_NODE=.., UNCOVERED=..
  - <issue edge>: <STATE>[ hops=<n>]
  ...
[Ranked inspection clues (computed, not model-chosen)]
  1. <candidate patch node> (for <issue edge>, status=<STATE>, pagerank=<x>)
  ...
```

The block is labelled as computed by graph algorithms and is inserted between
`========== 计算结果（图算法，非模型判断） ==========` and `========== 任务 ==========` in the
traceability prompt, and appended to the failure log in the failure-analysis prompt (paper §3.7).

## 7. Worked example

The following graph and edge set are the fixture used by the module's own unit test
(`trace_run/test_graph_refine.py`, section 4); the expected states are asserted there:

```
G_c:  read input -> validate -> store
      log -> audit
E_r:  (read input, store)   (validate, store)   (store, validate)   (ghost, store)
```

| Requirement edge | Test applied | State | Evidence |
|---|---|---|---|
| `read input -> store` | endpoints present; no direct edge; `read input→validate→store` | `PATH` | hops = 2 |
| `validate -> store` | endpoints present; direct edge exists | `DIRECT` | hops = 1 |
| `store -> validate` | endpoints present; no path | `UNCOVERED` | — |
| `ghost -> store` | `ghost ∉ V_c` | `NO_NODE` | missing endpoint `ghost` |

`U_t` for this fixture is `{(store, validate), (ghost, store)}`: exactly the two relations that
receive ranked clues.

> This table is derived from the rules and the test fixture; it is not a captured console run.
> `networkx` is not installed in the packaging environment, so the unit test itself was not
> re-executed while assembling this artifact. The states above are what the asserted test expects
> (`statuses == ["PATH", "DIRECT", "UNCOVERED", "NO_NODE"]`, `cov[0]["hops"] == 2`) and what the
> rules in §2 produce for that fixture.

## 8. Known limitations relevant to these rules

- **Exact name matching.** A requirement node and its implementation counterpart that are spelled
  differently are reported as `NO_NODE`. This is deliberate (it keeps the verdict auditable) and it
  is the main source of spurious gaps. A variant in the same experimental tree
  (`trace_run/trace_repair_b2.py`, generated by `trace_run/make_variant_b.py`) adds a deterministic
  alias map (`build_alias`, scored by name normalisation / token containment / token overlap) before
  the same four rules are applied; the paper states the rules with exact endpoint names (§3.4).
  The retained records do not record which variant produced each reported number.
- **Extraction, not static analysis.** `G_c^t` is what the model identified from the patch, not a
  call graph derived from the repository. "Reachable" means "reachable in the extracted graph".
- **Structure ≠ behaviour.** `DIRECT` is not a pass; the resolved verdict comes from the tests
  (paper §3.4, §4.2).

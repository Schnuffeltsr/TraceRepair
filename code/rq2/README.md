# RQ2: human evaluation

RQ2 was assessed by manual annotation; no automation script was used to produce the reported
percentages. This directory therefore contains the protocol description only.

The annotation unit is a single relation:

- **issue edge** (a requirement relation): judged valid when the issue text supports the two
  endpoints and the direction; denominators are the annotated edges (440).
- **patch edge** (an implementation relation): judged supported when the code fragment supports
  the relation; denominators are 520.
- **cross-graph mapping**: judged under a strict and a lenient criterion, with 355 mappings
  annotated. The lenient criterion accepts strictly-accepted mappings, so the two are not
  mutually exclusive; 75 mappings fail even the lenient criterion.

The aggregated counts and the resulting percentages are in `RQ2/rq2_human_eval.csv` at the
repository root. Annotator count, disagreement handling and agreement statistics were not
recorded, which is stated as a limitation in the paper.

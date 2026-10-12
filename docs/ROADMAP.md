# Sentinel roadmap

Phases 1–9 are implemented. The [Phase 9 design](superpowers/specs/2026-10-07-phase9-reviewer-ui-design.md)
defines the reviewer interface boundary. Earlier specs and progress logs record
the phase numbering in use when they were written; this page summarizes the
final local portfolio scope.

| Phase | Outcome | Boundary |
|---|---|---|
| 6 | Reproducible offline features, Isolation Forest training, and held-out evaluation on original source rows | No live scoring or case changes |
| 7 | Optional model inference for cases already flagged by rules, with artifact compatibility checks and `ml_anomaly_score` persistence | Model never decides whether to flag |
| 8 | Optional plain-English case summaries after rule-based flagging | Summaries never decide whether to flag |
| 9 | Reviewer UI for the existing queue, detail, feedback, rule controls, and live updates | Uses the existing REST/WebSocket contract |

The local portfolio scope is complete. CI covers the backend suite, real
API/pipeline smoke test, frontend tests/build, and offline data/ML tests.
There is no deployment target or login system: the reviewer name is an
attribution label in this local demo. Future deployment, authentication,
automated partition/rollup maintenance, and higher-throughput reliability
work would require separate scope decisions. See [the project guide](PROJECT_GUIDE.md)
for architecture, run instructions, file inventory, and candid limits.

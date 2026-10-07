# Sentinel roadmap

Phases 1–7 are implemented. The [Phase 7 design](superpowers/specs/2026-10-07-phase7-case-annotation-design.md)
defines the optional model boundary. Earlier specs and progress logs record
the phase numbering in use when they were written; this page is the current
allocation for the remaining work.

| Phase | Outcome | Boundary |
|---|---|---|
| 6 | Reproducible offline features, Isolation Forest training, and held-out evaluation on original source rows | No live scoring or case changes |
| 7 | Optional model inference for cases already flagged by rules, with artifact compatibility checks and `ml_anomaly_score` persistence | Model never decides whether to flag |
| 8 | Optional plain-English case summaries after rule-based flagging | Summaries never decide whether to flag |
| 9 | Reviewer UI for the existing queue, detail, feedback, rule controls, and live updates | Uses the existing REST/WebSocket contract |

Deployment, CI, authentication, and deferred pipeline maintenance need their
own scope decisions; this page does not assign them a phase number.

# Identity-safe cleanup correction plan

> Execute locally only. Do not invoke model, MCP, Saxo, broker, browser, network, install, or proof workflows.

1. Add deterministic RED regressions for PID reuse between selection and signal, reused group leaders, late group members, survivor/zombie/unknown coverage, nested eval reuse, exact-install reuse, and strict child-failure cleanup evidence.
2. Introduce one shared birth-bound cleanup primitive that captures current group members before signalling, rechecks identity inside every signal operation, and performs a terminal coverage rescan.
3. Migrate the command runner, native eval manager, and exact install cleanup/audit paths away from raw historical PID/PGID signalling and counting.
4. Make child-failure verification fail closed unless cleanup evidence is authenticated or proves that no target was observed, with consistent digest and known complete semantic counts.
5. Run focused tests twice, broader local auth/privacy tests, Ruff/format, BasedPyright, plugin/static/catalog/eval/privacy checks, then update tracked docs, ignored Task 24 evidence, and existing Knowledge Base rows with readback.
6. Commit the verified source and documentation, report exact SHA/tree/clean state, and stop before install or proof.

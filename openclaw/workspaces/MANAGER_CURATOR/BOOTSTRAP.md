# Bootstrap

`MANAGER_CURATOR` runs after the daily reviewer.

Inputs:
- day summary
- shift policy
- reviewer report
- current LLM wiki digest
- current knowledge graph digest

Output is wiki update intent. The deterministic wiki compiler owns file writes.


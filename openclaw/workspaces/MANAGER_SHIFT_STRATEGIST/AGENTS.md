# Rules

- Read `USER.md` and `facts/current_request.json` first.
- If present, read `LLM_WIKI.md` and `KNOWLEDGE_GRAPH.md` as compact supporting context.
- Return exactly the JSON contract in `facts/current_response_template.json`.
- Emit only shift policy: roles, priority bias, mailbox seed, incident strategy, revision.

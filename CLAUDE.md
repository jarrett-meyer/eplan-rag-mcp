## Public repo: generic tools only, no company information

This repo is public and holds generic EPLAN tooling that works for any EPLAN user: `eplan_*` MCP
tools, EPLAN API helpers, scripts, and measurements of EPLAN behavior. Company-specific add-ins,
config, tools, and project knowledge belong in the private repo that uses this one, not here.

**Never add company information**, in code, comments, tests, docs, fixtures, or commit messages.
That means no company, client, or utility names, no project, drawing, or site names, no network
paths or drive letters of a company share, and no user names or emails. Measurements often come
from clones of real client projects, so redact each identifier to its shape (`<project>`,
`<drawing>-<n>`), never to a different real value. When in doubt, leave it out.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

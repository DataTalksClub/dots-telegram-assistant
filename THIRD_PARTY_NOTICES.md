# Design references and attribution

The Telegram event-source pattern was informed by the public
[panyam/mcpkit Telegram Events example](https://github.com/panyam/mcpkit/tree/main/examples/events/telegram),
particularly its event registration, Telegram ingestion, and message-reading separation.
The upstream project is by Sriram Panyam and contributors; its
[license](https://github.com/panyam/mcpkit/blob/main/LICENSE) is Apache License 2.0
and includes Copyright 2026 Sriram Panyam. Reviewed on 2026-10-13.

This repository's Python transport is a new implementation. No upstream Go code,
SDK, or bundled source is copied or redistributed. This notice credits design
reference; it does not claim upstream endorsement, compatibility with every
experimental extension, or transfer the upstream license to unrelated original code.

For reproducibility, the reviewed upstream files had these Git blob identifiers:

- `examples/events/telegram/README.md`: `3f9ede6914cc460ed06f3999b18aa6c307cb7159`
- `examples/events/telegram/events.go`: `9661c0d844f455478b9eb38d862b4731a08faffa`
- `examples/events/telegram/handlers.go`: `5d2a7235c5b148ead0260da426a30d66750a255a`

The upstream README describes its Events extension as experimental. This project
implements only its documented, narrower MCP Events transport surface; upstream
examples and their authentication shortcuts are not production setup instructions
for this service.

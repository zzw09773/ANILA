"""AgenticRAG CLI surface.

Entry point: ``python -m agentic_rag.cli <subcommand> [args...]``.

Subcommands:

- ``bootstrap`` — exchange a CSP-issued bsk- bootstrap token for a
  long-lived csk- service token and write it to the agent state file.
  See ``docs/csp-agent-bootstrap-protocol.md`` for the wire contract.

History: the bootstrap CLI used to live at
``anila-core agent bootstrap`` and AgenticRAG's container entrypoint
shelled out to it. Phase 0 decoupling (2026-05-02) brought a copy
back into AgenticRAG so devs forking this template don't need to
install the platform-internal anila-core package.
"""

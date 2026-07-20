# Framework adapters

`langchain_timeline_adapter.py` is the supported LangChain callback example for
Gate 4. It emits the same named `event: anila.step` / `step-event/v1` wire
contract as the official OpenAI Agents SDK runtime. CSP remains the trust
boundary: it validates the schema, overwrites task/trace/agent/session/run
identity from dispatch state, and applies event budgets before browser delivery.

The adapter deliberately does not copy prompts, tool arguments, retrieval
queries/documents, chain output, or reasoning into the timeline. Applications
should pass the callback's frames to their SSE response unchanged; they must not
invent additional `anila.*` event names.

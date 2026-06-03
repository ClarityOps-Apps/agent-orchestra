"""LLM provider adapters for Agent Orchestra agents.

Per M4 architecture scope (Asana comment 1215386979487630): provider adapter
pattern — `OpenAIProvider` for Atlas, `AnthropicProvider` for Cody/Scribe/Scout
(task 4.2). Each provider loads a locked system prompt and exposes a single
`send(messages, tools=None) -> AgentResponse` shape so the agent factory (4.3),
message-passing protocol (4.4), and supervisor loop (4.5) only need to know
the interface, not the SDK.
"""

"""Agent runtime adapters.

The deterministic runtimes exist so orchestration can be verified without a live model. A real
LLM runtime is a later adapter behind the same port; the kernel does not change to accommodate
either one.
"""

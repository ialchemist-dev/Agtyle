"""Pure domain layer.

Modules here must not import FastAPI, SQLAlchemy, subprocess-based authorization,
filesystem access or LLM SDKs. They express business invariants only.
"""

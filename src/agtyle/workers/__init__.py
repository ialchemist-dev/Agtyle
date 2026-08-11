"""Process roles.

Workers are composition and loop control only. Every decision they make is delegated to an
application service, so the same behavior is exercised by tests that never start a process.
"""

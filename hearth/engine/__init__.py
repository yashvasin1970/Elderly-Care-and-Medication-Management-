"""Hearth's decision engine.

Everything in this package is pure Python: no Flask, no database, no clock
of its own. Callers pass in plain values (including the current time) and get
plain values back, which keeps every care rule easy to read and to test.
"""

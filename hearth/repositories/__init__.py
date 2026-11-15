"""Database access.

Each module owns the SQL for one area. Functions take a connection, use
parameterised queries only, and always filter by ``profile_id`` so one care
circle can never read or change another circle's data.
"""

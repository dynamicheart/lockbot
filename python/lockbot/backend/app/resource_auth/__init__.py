"""Resource-auth package: verified IM identity binding for the agent-facing API.

Layering rule for this package: it sees the IM platform only as an opaque
``(platform, external_user_id)`` pair. No module here may reference Infoflow, a
platform-specific field name, or a platform payload shape.
"""

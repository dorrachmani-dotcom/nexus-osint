"""FastAPI routers for the web UI, grouped by domain.

``nexus.web.app`` includes them in ``ROUTERS`` order; that order preserves the
original first-match precedence of overlapping URL patterns (see
``tests/test_route_order.py``).
"""

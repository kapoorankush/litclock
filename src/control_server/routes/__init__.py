"""Route registration root for the LitClock Control PWA.

Each blueprint lives in its own module under this package. The factory in
``src/control_server/__init__.py:create_app`` imports + registers them. M1
ships ``index`` (PWA shell) and ``health`` (post-restart reconnect probe);
M2-M5 add ``status``, ``settings``, ``system``, ``updates``, ``wifi``.
"""

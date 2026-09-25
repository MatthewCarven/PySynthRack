"""DearPyGui UI.

``App`` and ``main`` are resolved lazily so the dpg-free helpers in this
package (``zoom``, ``buffer``, ``scope_math``, ...) import without DearPyGui
installed -- the headless tests depend on that.
"""

__all__ = ["App", "main"]


def __getattr__(name):
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

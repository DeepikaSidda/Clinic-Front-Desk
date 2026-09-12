"""Dashboard web components (task 13.x).

Server-rendered view-model builders and HTML renderers for the role-aware
dashboard. Each component is a pure Python module that shapes data read through
the Data_Layer into a view-model and renders it to HTML, paired with a static
template and a thin vanilla-JS progressive-enhancement snippet under
``dashboard/web/``.

This package intentionally performs no imports at package-import time so that
sibling components can be developed and imported independently.
"""

# Reader demos

Maintain the two notebooks as independent, top-to-bottom examples for ЗУП КОРП 3.1.38.92 and the 01.08.2021 data snapshot. Keep `01-overview` and `03-capture` session startup consistent. Use separate `%%bsl` cells for reader-facing BSL calls and Python cells for inspection, including stack access. Use path-and-line breakpoint methods (`add_capture_point`, `clear_capture_points`). Do not import `demo_support` or assert totals from the old 3.1.38.41 snapshot.

Edit `tools/build_demo_notebooks.py` as the source of generated notebook cells. Regenerate both notebooks, validate with `nbformat`, and clear outputs and execution counts before committing. A static pass does not establish that a demo ran against a live 1C base.

"""Pure logic layer.

Nothing in `core` may import from `net`, `sw`, or `ui`, and nothing here touches the network
or SolidWorks. That restriction is what makes the whole model testable without a second
machine, a hotspot, or a SolidWorks licence — see docs/01-MIMARI-TASLAK.md section 9.
"""

"""Selection modes and Krita's modifier-key conventions (no Qt/Krita imports).

Mirrors KisSelectionModifierMapper so SAM Select reacts to modifiers exactly
like the built-in selection tools, including the "Switch Control/Alt
Selection Modifiers" preference. On macOS Qt maps Command to Control and
Option to Alt.
"""

from __future__ import annotations

REPLACE = "replace"
INTERSECT = "intersect"
ADD = "add"
SUBTRACT = "subtract"
SYMMETRIC_DIFFERENCE = "symmetric_difference"

# Order and icons match Krita's selection tool options.
MODES = [REPLACE, INTERSECT, ADD, SUBTRACT, SYMMETRIC_DIFFERENCE]
LABELS = {
    REPLACE: "Replace",
    INTERSECT: "Intersect",
    ADD: "Add",
    SUBTRACT: "Subtract",
    SYMMETRIC_DIFFERENCE: "Symmetric Difference",
}
ICONS = {
    REPLACE: "selection_replace",
    INTERSECT: "selection_intersect",
    ADD: "selection_add",
    SUBTRACT: "selection_subtract",
    SYMMETRIC_DIFFERENCE: "selection_symmetric_difference",
}
# Krita actions that switch the active selection tool's mode.
MODE_ACTIONS = {
    "selection_tool_mode_replace": REPLACE,
    "selection_tool_mode_intersect": INTERSECT,
    "selection_tool_mode_add": ADD,
    "selection_tool_mode_subtract": SUBTRACT,
}

# Qt::KeyboardModifier bits (kept numeric so this module stays Qt-free).
SHIFT = 0x02000000
CONTROL = 0x04000000  # Command on macOS
ALT = 0x08000000  # Option on macOS
META = 0x10000000  # Control on macOS
_RELEVANT = SHIFT | CONTROL | ALT


def mode_for_modifiers(modifiers: int, default: str, swap_ctrl_alt: bool = False) -> str:
    """Selection mode for the modifiers held when an action starts."""
    m = modifiers & _RELEVANT
    replace, subtract = (ALT, CONTROL) if swap_ctrl_alt else (CONTROL, ALT)
    table = {
        replace: REPLACE,
        SHIFT: ADD,
        subtract: SUBTRACT,
        SHIFT | subtract: INTERSECT,
        CONTROL | ALT: SYMMETRIC_DIFFERENCE,
    }
    return table.get(m, default)


def modifier_hint(swap_ctrl_alt: bool = False) -> str:
    sub, rep = ("⌘", "⌥") if swap_ctrl_alt else ("⌥", "⌘")
    return f"⇧ add · {sub} subtract · ⇧{sub} intersect · {rep} replace · ⌘⌥ symmetric difference"

"""Tool options, stored in kritarc the same way Krita's selection tools do.

* The selection mode lives in the shared ``[KisToolSelectBase] selectionAction``
  key, so switching between SAM Select and any built-in selection tool keeps
  the mode, exactly as switching between two built-in tools does.
* Anti-aliasing, grow/shrink, feather and the sample-layers reference reuse
  Krita's per-tool key names and values, under our own ``[SamSelect]`` group.
"""

from __future__ import annotations

from krita import Krita

from . import modes

GROUP = "SamSelect"
SHARED_GROUP = "KisToolSelectBase"

# SelectionAction enum values (libs/image/KisSelectionTags.h).
_ACTION_TO_MODE = {0: modes.REPLACE, 1: modes.ADD, 2: modes.SUBTRACT, 3: modes.INTERSECT, 4: modes.SYMMETRIC_DIFFERENCE}
_MODE_TO_ACTION = {v: k for k, v in _ACTION_TO_MODE.items()}

SAMPLE_ALL = "sampleAllLayers"
SAMPLE_CURRENT = "sampleCurrentLayer"

DEFAULTS = {
    "antiAliasSelection": True,
    "growSelection": 0,  # px, negative shrinks
    "featherSelection": 0,  # px
    "sampleLayersMode": SAMPLE_ALL,
    "dragSelectsAllObjects": True,  # drag = every object in the box; False = the box's main object
    "textThreshold": 0.5,
    "hoverPreview": True,
    "idleUnloadMinutes": 20,
}


def _read(group: str, key: str, default):
    raw = Krita.instance().readSetting(group, key, "")
    if raw == "":
        return default
    try:
        if isinstance(default, bool):
            return raw.lower() == "true"
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def _write(group: str, key: str, value) -> None:
    Krita.instance().writeSetting(group, key, str(value).lower() if isinstance(value, bool) else str(value))


def get(key: str):
    return _read(GROUP, key, DEFAULTS[key])


def put(key: str, value) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    _write(GROUP, key, value)


def mode() -> str:
    return _ACTION_TO_MODE.get(_read(SHARED_GROUP, "selectionAction", 0), modes.REPLACE)


def set_mode(value: str) -> None:
    _write(SHARED_GROUP, "selectionAction", _MODE_TO_ACTION[value])


def swap_ctrl_alt() -> bool:
    """Krita's "Switch Control/Alt Selection Modifiers" preference."""
    return _read("", "switchSelectionCtrlAlt", False)

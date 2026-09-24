"""Turn a mask from the backend into an undoable Krita selection change.

Mirrors what Krita's selection tools do: the adjustments (grow/shrink,
feather) apply to the *new* shape, which is then combined with the existing
global selection using the selection mode.

Two libkis details drive the implementation (see Krita's libs/libkis and
libs/image sources):

* ``Document.selection()`` wraps the *live* selection, and ``setSelection``
  adopts the object it is given. So we always build a fresh object (or a
  ``duplicate()``) and never touch it after handing it over. That keeps the
  change a single undo step (``KisSetGlobalSelectionCommand``).
* ``Selection.setPixelData`` does not invalidate the marching-ants outline
  cache: a fresh selection claims a *valid, empty* outline, so Krita would show
  no ants. Inverting twice inside real image bounds invalidates the cache
  without changing any pixel, and Krita then recomputes the outline itself.
  A detached ``SelectionMask`` gives a new selection those image bounds.
"""

from __future__ import annotations

from krita import Selection
from PyQt5.QtCore import QByteArray

from . import modes


def _bind_to_image(doc, sel: Selection) -> Selection:
    """Give a free-standing Selection the document's default bounds."""
    mask = doc.createSelectionMask("SAM Select (temporary)")
    if mask is not None:
        mask.setSelection(sel)  # KisMask::setSelection -> KisDefaultBounds(image)
    return sel


def _refresh_outline(sel: Selection) -> None:
    sel.invert()
    sel.invert()


def apply_mask(doc, x: int, y: int, w: int, h: int, data: bytes, mode: str, grow: int = 0, feather: int = 0) -> bool:
    """Combine an 8-bit mask (``w * h`` bytes at ``x, y``) into the global selection.

    Returns False when the operation had no effect (e.g. subtracting with no selection).
    """
    current = doc.selection()
    if current is not None and current.width() == 0 and current.height() == 0:
        current = None
    if current is None and mode == modes.SUBTRACT:
        return False

    new = _bind_to_image(doc, Selection())
    new.setPixelData(QByteArray(data), x, y, w, h)
    if grow > 0:
        new.grow(grow, grow)
    elif grow < 0:
        new.shrink(-grow, -grow, False)
    if feather > 0:
        new.feather(feather)
    if grow > 0 or feather > 0:
        # Keep everything inside the canvas, like the built-in tools.
        bounds = _bind_to_image(doc, Selection())
        bounds.select(0, 0, doc.width(), doc.height(), 255)
        new.intersect(bounds)

    if current is None or mode == modes.REPLACE:
        result = new
    else:
        result = current.duplicate()  # keeps the image's default bounds
        if mode == modes.ADD:
            result.add(new)
        elif mode == modes.SUBTRACT:
            result.subtract(new)
        elif mode == modes.INTERSECT:
            result.intersect(new)
        elif mode == modes.SYMMETRIC_DIFFERENCE:
            result.symmetricdifference(new)
        else:
            result = new

    _refresh_outline(result)
    if result.width() == 0 or result.height() == 0:
        if current is None:
            return False
        doc.setSelection(None)  # e.g. an intersect with nothing in common: deselect, undoably
    else:
        doc.setSelection(result)
    return True

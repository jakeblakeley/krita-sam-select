"""SAM Select for Krita: object selection with Segment Anything 3 (MLX, Apple Silicon)."""

from krita import Krita

from .extension import SamSelectExtension
from .version import __version__  # noqa: F401

Krita.instance().addExtension(SamSelectExtension(Krita.instance()))

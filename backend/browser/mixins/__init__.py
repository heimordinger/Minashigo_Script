# backend/browser/mixins/__init__.py
from .base import BaseMixin
from .lifecycle import LifecycleMixin
from .navigation import NavigationMixin
from .mouse_actions import MouseActionsMixin
from .slide_actions import SlideActionsMixin
from .scroll_actions import ScrollActionsMixin
from .keyboard_actions import KeyboardActionsMixin
from .image_matching import ImageMatchingMixin
from .text_matching import TextMatchingMixin
from .waiting import WaitingMixin
from .debug import DebugMixin
from .utils import UtilsMixin
from .multi_step import MultiStepMixin

__all__ = [
    'BaseMixin',
    'LifecycleMixin',
    'NavigationMixin',
    'MouseActionsMixin',
    'SlideActionsMixin',
    'ScrollActionsMixin',
    'KeyboardActionsMixin',
    'ImageMatchingMixin',
    'TextMatchingMixin',
    'WaitingMixin',
    'DebugMixin',
    'UtilsMixin',
    'MultiStepMixin',
]
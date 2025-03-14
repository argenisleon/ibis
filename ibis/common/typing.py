import sys
from typing import Any, ForwardRef

import toolz

if sys.version_info >= (3, 9):

    @toolz.memoize
    def evaluate_typehint(hint, module_name) -> Any:
        if isinstance(hint, str):
            hint = ForwardRef(hint)
        if isinstance(hint, ForwardRef):
            globalns = sys.modules[module_name].__dict__
            return hint._evaluate(globalns, locals(), frozenset())
        else:
            return hint

else:

    @toolz.memoize
    def evaluate_typehint(hint, module_name) -> Any:
        if isinstance(hint, str):
            hint = ForwardRef(hint)
        if isinstance(hint, ForwardRef):
            globalns = sys.modules[module_name].__dict__
            return hint._evaluate(globalns, locals())
        else:
            return hint

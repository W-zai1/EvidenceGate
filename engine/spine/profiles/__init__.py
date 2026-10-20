"""profile 注册表。注册即扩展，脊柱不感知具体场景。"""
from engine.spine.profiles.base import Profile


class _Registry:
    def __init__(self):
        self._items = {}

    def register(self, profile):
        self._items[profile.key] = profile
        return profile

    def get(self, key):
        if key not in self._items:
            raise KeyError(key)
        return self._items[key]

    def labels(self):
        return ["%s@%s" % (k, v.version) for k, v in self._items.items()]

    def keys(self):
        return list(self._items.keys())


registry = _Registry()

# 注册内置场景 profile。
from engine.spine.profiles.expense import build as _build_expense
from engine.spine.profiles.talent import build as _build_talent
registry.register(_build_expense())
registry.register(_build_talent())

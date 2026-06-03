import json
import os

POSITIONS_FILE = "/tmp/open_positions.json"

def _load():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def _save():
    with open(POSITIONS_FILE, "w") as f:
        json.dump(open_positions, f)

class _Positions(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        _save()

    def __delitem__(self, key):
        super().__delitem__(key)
        _save()

open_positions = _Positions(_load())
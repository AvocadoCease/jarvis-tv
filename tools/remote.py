"""Press buttons on the TV from the command line.

  python3 tools/remote.py KEY_EXIT
  python3 tools/remote.py KEY_RETURN KEY_LEFT KEY_ENTER
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works under python -I
import tvcontrol  # noqa: E402

if __name__ == "__main__":
    tvcontrol.keys(sys.argv[1:] or ["KEY_EXIT"])
    print("sent", " ".join(sys.argv[1:] or ["KEY_EXIT"]))

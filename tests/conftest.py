import pathlib
import sys

# Make repo root importable so `shared`, `notebooks`, and `scripts` packages resolve.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

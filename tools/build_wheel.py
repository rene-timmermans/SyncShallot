"""Build a wheel locally without invoking pip or accessing the network."""
import os
from pathlib import Path
from setuptools.build_meta import build_wheel

os.chdir(Path(__file__).resolve().parents[1])
print(build_wheel("dist"))

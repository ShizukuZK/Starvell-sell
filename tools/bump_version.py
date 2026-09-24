"""Меняет номер версии в version.py: python tools/bump_version.py 2.1.1"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        print("формат: python tools/bump_version.py 2.1.1")
        return 1
    path = os.path.join(ROOT, "version.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'VERSION = "[^"]*"', f'VERSION = "{sys.argv[1]}"', text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"version.py → {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Load only the trusted action source while using isolated Python mode."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pr_check import main

sys.exit(main())

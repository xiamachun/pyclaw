"""Allow running PyClaw as a module: python -m pyclaw."""

from pyclaw.cli.main import cli

if __name__ == "__main__":
    cli()

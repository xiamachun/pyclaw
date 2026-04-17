"""
Main CLI entry point for pyclaw.
"""

import click

from pyclaw.cli.config_cmd import config

@click.group("pyclaw", help="PyClaw - AI Agent Gateway")
def cli():
    """PyClaw CLI for managing AI agents and gateway."""
    pass

# Register subcommands
cli.add_command(config)

if __name__ == '__main__':
    cli()
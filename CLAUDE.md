# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

recall-be is a Python backend application currently in early development stage.

## Environment Setup

- Python version: 3.13+
- Project uses `uv` for dependency management (based on pyproject.toml structure)

## Development Commands

### Running the application
```bash
python main.py
```

### Installing dependencies
```bash
uv sync
```

### Managing dependencies
```bash
uv add <package>        # Add a new dependency
uv remove <package>     # Remove a dependency
```

## Project Structure

Currently minimal structure:
- `main.py` - Application entry point
- `pyproject.toml` - Project configuration and dependencies

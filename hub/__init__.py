"""The Riko hub: shared brain, memory and personas.

Importable two ways, same code:
  * as the package ``hub`` from the repo root (local mode in-process:
    ``from hub.server import create_app``)
  * as bare modules when run as ``python server.py`` inside this folder
    (the Docker image); the sibling imports in server.py fall back to that.
"""

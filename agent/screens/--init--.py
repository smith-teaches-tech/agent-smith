"""
agent.screens — per-screen modules.

Each screen has its own file (e.g. ai_sympathy.py for Screen 1) that
exports the functions main.py needs to run that screen end-to-end.

Adding a new screen:
  1. Create agent/screens/<screen_name>.py with the screen's logic.
  2. Add an entry to config.SCREENS.
  3. Wire it into main.py's orchestrator.
"""
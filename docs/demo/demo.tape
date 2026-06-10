# VHS tape for the aGiT usage demo (docs/assets/demo.gif).
#
# Reproducible recording: when the UI changes, re-run this instead of screen
# capturing by hand. Requires: vhs (brew install vhs), agit on PATH, the
# `claude` backend installed and authenticated, and a scratch repo prepared by
# docs/demo/prepare.sh (which this tape runs first).
#
#   cd docs/demo && vhs demo.tape
#
# Timings are generous because a real agent answers the prompt; expect the
# recording to take a few minutes and trim via the final Sleep if needed.

Output ../assets/demo.gif

Set FontFamily "IBM Plex Mono"
Set FontSize 17
Set Width 1080
Set Height 640
Set Theme "Catppuccin Mocha"
Set Padding 12
Set TypingSpeed 60ms

Hide
# export here: an export inside prepare.sh would not reach this shell
Type "./prepare.sh && export AGIT_CONFIG_DIR=/tmp/agit-demo-config && cd /tmp/agit-demo && clear"
Enter
Sleep 3s
Show

# Launch aGiT (resumes nothing: prepare.sh gives a fresh repo + fresh config)
Type "agit"
Enter
Sleep 12s

# Ask the agent for a small, visually clear change.
Type "add a greet(name) function with a docstring to hello.py"
Sleep 1s
Enter

# Let the agent work and aGiT's idle debounce fire the auto-commit popup.
Sleep 75s

# Show the aGiT menu briefly (the configurable menu key, default Ctrl-G).
Ctrl+G
Sleep 3s
Escape
Sleep 2s

# Exit aGiT: Ctrl-C opens the confirm popup; Enter confirms the highlighted
# "No, keep working" -> use Down+Enter to pick "Yes, exit".
Ctrl+C
Sleep 2s
Down
Enter
Sleep 6s

# The payoff: the agent's turn is a real commit with the trace in the body.
Type "git log --format='%h %s' -3"
Enter
Sleep 4s
Type "git show --stat HEAD | head -30"
Enter
Sleep 8s

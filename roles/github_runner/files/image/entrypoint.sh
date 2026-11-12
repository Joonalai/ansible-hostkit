#!/bin/sh
# The JIT config arrives in the environment from the unit's env file, and
# stays there: it carries the runner's credentials for as long as the runner
# lives, and a command line is readable by every account on the box, while
# an environment is coder's and root's alone. The runner reads
# ACTIONS_RUNNER_INPUT_JITCONFIG as it would --jitconfig, and removes it from
# its environment before any job step can inherit it.
set -eu
: "${RUNNER_JITCONFIG:?no JIT config; the unit mints one before starting the container}"
export ACTIONS_RUNNER_INPUT_JITCONFIG="$RUNNER_JITCONFIG"
unset RUNNER_JITCONFIG
exec ./run.sh

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly SCRIPT_DIR

ensure_symlink() {
  local source="$1"
  local target="$2"

  mkdir -p "$(dirname "$target")"
  if [[ -L "$target" ]] && [[ "$(readlink "$target")" == "$source" ]]; then
    echo "  unchanged link → ${target}"
    return
  fi

  ln -sfn "$source" "$target"
  echo "  linked → ${target}"
}

prepare_skill_directory() {
  local target="$1"

  if [[ -L "$target" ]]; then
    echo "skill directory is a symlink: ${target}" >&2
    return 1
  fi
  if [[ -e "$target" ]] && [[ ! -d "$target" ]]; then
    echo "skill directory target is not a directory: ${target}" >&2
    return 1
  fi

  mkdir -p "$target"
}

link_agents_md() {
  local targets=(
    ~/.claude/CLAUDE.md
    ~/.codex/AGENTS.md
    ~/.gemini/GEMINI.md
    ~/.kimi-code/AGENTS.md
  )
  echo "Installing AGENTS.md → agent config symlinks..."
  for target in "${targets[@]}"; do
    ensure_symlink "${SCRIPT_DIR}/AGENTS.md" "$target"
  done
}

prepare_skill_paths() {
  local targets=(
    ~/.agents/skills
    ~/.claude/skills
  )

  echo "Preparing skill directories..."
  for target in "${targets[@]}"; do
    prepare_skill_directory "$target"
  done
}

link_hooks() {
  local source="${SCRIPT_DIR}/scripts/hooks/session-start-task-manager.py"
  local targets=(
    ~/.claude/hooks/session-start-task-manager.py
    ~/.codex/hooks/session-start-task-manager.py
  )
  echo "Installing client hook symlinks..."
  for target in "${targets[@]}"; do
    ensure_symlink "$source" "$target"
  done
}

run_project_script() {
  local script="$1"
  shift

  if command -v uv >/dev/null 2>&1; then
    uv run --locked --project "${SCRIPT_DIR}" \
      "${SCRIPT_DIR}/${script}" "$@"
  elif [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/${script}" "$@"
  else
    echo "Unable to run ${script}: run 'uv sync --locked' first." >&2
    return 1
  fi
}

sync_agent_configs() {
  local answer
  if read -r -p "Sync repository config patches to agent configs? [y/N] " answer &&
    [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]; then
    echo "Syncing agent configs..."
    run_project_script "scripts/sync-configs.py"
  else
    echo "Skipped agent config sync."
  fi
}

install_skills() {
  local answer
  if read -r -p "Install missing skills and update installed skills? [y/N] " answer &&
    [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]; then
    echo "Installing missing skills and updating installed skills..."
    run_project_script "scripts/install-skills.py" --update --apply
  else
    echo "Skipped skill installation."
  fi
}

main() {
  link_agents_md
  echo ""
  prepare_skill_paths
  echo ""
  link_hooks
  echo ""
  sync_agent_configs
  echo ""
  install_skills
}

main "$@"

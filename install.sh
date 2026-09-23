#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly SCRIPT_DIR

COLOR_RESET='' COLOR_CYAN='' COLOR_GREEN='' COLOR_YELLOW='' COLOR_RED='' COLOR_DIM=''
if [[ -z ${NO_COLOR+x} ]] &&
  { [[ ${CLICOLOR_FORCE:-} == 1 ]] || [[ -t 1 && ${TERM:-dumb} != dumb ]]; }; then
  COLOR_RESET=$'\033[0m'
  COLOR_CYAN=$'\033[1;36m'
  COLOR_GREEN=$'\033[32m'
  COLOR_YELLOW=$'\033[33m'
  COLOR_RED=$'\033[31m'
  COLOR_DIM=$'\033[2m'
fi

say_color() {
  local color="$1"
  shift
  printf '%s%s%s\n' "$color" "$*" "$COLOR_RESET"
}

ensure_symlink() {
  local source="$1"
  local target="$2"
  local answer backup_dir

  mkdir -p "$(dirname "$target")"
  if [[ -L "$target" && "$target" -ef "$source" ]]; then
    say_color "$COLOR_DIM" "  unchanged link → ${target}"
    return
  fi

  if [[ -e "$target" || -L "$target" ]]; then
    printf '%sOverwrite existing %s? [y/N]%s ' "$COLOR_YELLOW" "$target" "$COLOR_RESET"
    if ! read -r answer ||
      [[ ! "$answer" =~ ^([yY]|[yY][eE][sS])$ ]]; then
      say_color "$COLOR_YELLOW" "  skipped existing target → ${target}"
      return
    fi

    backup_dir="$(mktemp -d "${target}.backup.XXXXXX")"
    if ! mv "$target" "${backup_dir}/original"; then
      rmdir "$backup_dir"
      return 1
    fi
    if ! ln -s "$source" "$target"; then
      mv "${backup_dir}/original" "$target"
      rmdir "$backup_dir"
      return 1
    fi
    say_color "$COLOR_CYAN" "  saved original → ${backup_dir}/original"
  else
    ln -s "$source" "$target"
  fi
  say_color "$COLOR_GREEN" "  linked → ${target}"
}

check_skill_directory() {
  local target="$1"

  if [[ -L "$target" ]]; then
    printf '%sskill directory is a symlink: %s%s\n' "$COLOR_RED" "$target" "$COLOR_RESET" >&2
    return 1
  fi
  if [[ -e "$target" ]] && [[ ! -d "$target" ]]; then
    printf '%sskill directory target is not a directory: %s%s\n' "$COLOR_RED" "$target" "$COLOR_RESET" >&2
    return 1
  fi
}

link_agents_md() {
  local targets=(
    ~/.claude/CLAUDE.md
    ~/.codex/AGENTS.md
    ~/.gemini/GEMINI.md
    ~/.kimi-code/AGENTS.md
  )
  say_color "$COLOR_CYAN" "Installing AGENTS.md → agent config symlinks..."
  for target in "${targets[@]}"; do
    ensure_symlink "${SCRIPT_DIR}/AGENTS.md" "$target"
  done
}

prepare_skill_paths() {
  local targets=(
    ~/.agents/skills
    ~/.claude/skills
  )

  say_color "$COLOR_CYAN" "Preparing skill directories..."
  for target in "${targets[@]}"; do
    check_skill_directory "$target"
  done
  for target in "${targets[@]}"; do
    mkdir -p "$target"
  done
}

link_hooks() {
  local source="${SCRIPT_DIR}/hooks/session-start-task-manager.py"
  local targets=(
    ~/.claude/hooks/session-start-task-manager.py
    ~/.codex/hooks/session-start-task-manager.py
  )
  say_color "$COLOR_CYAN" "Installing client hook symlinks..."
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
    printf "%sUnable to run %s: run 'uv sync --locked' first.%s\n" "$COLOR_RED" "$script" "$COLOR_RESET" >&2
    return 1
  fi
}

sync_agent_configs() {
  local answer
  if read -r -p "${COLOR_YELLOW}Preview repository config changes? [y/N] ${COLOR_RESET}" answer &&
    [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]; then
    say_color "$COLOR_CYAN" "Previewing agent config changes..."
    run_project_script "scripts/sync-configs.py" --preview
    if read -r -p "${COLOR_YELLOW}Apply these config changes? [y/N] ${COLOR_RESET}" answer &&
      [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]; then
      say_color "$COLOR_CYAN" "Syncing agent configs..."
      run_project_script "scripts/sync-configs.py"
    else
      say_color "$COLOR_YELLOW" "Skipped agent config sync."
    fi
  else
    say_color "$COLOR_YELLOW" "Skipped agent config sync."
  fi
}

install_skills() {
  local answer
  if read -r -p "${COLOR_YELLOW}Install missing skills and update installed skills? [y/N] ${COLOR_RESET}" answer &&
    [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]; then
    say_color "$COLOR_CYAN" "Installing missing skills and updating installed skills..."
    run_project_script "scripts/install-skills.py" --update --apply
  else
    say_color "$COLOR_YELLOW" "Skipped skill installation."
  fi
}

main() {
  prepare_skill_paths
  echo ""
  link_agents_md
  echo ""
  link_hooks
  echo ""
  sync_agent_configs
  echo ""
  install_skills
}

main "$@"

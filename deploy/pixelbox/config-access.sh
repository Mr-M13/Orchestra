#!/usr/bin/env bash
# Narrow configuration-directory ACL helper for the PixelBox installer.
#
# The dedicated GitHub dispatch broker runs as a different identity and needs
# execute (traverse) permission on the configuration root to reach its own
# 0700 configuration directory.  It must not be able to list the root or read
# the coordinator's owner-only secrets.
#
# `install -d -m 0700` rewrites the ACL mask on an existing directory, which
# silently disables a previously effective named entry.  Call this helper after
# every such operation, including repeated staging runs.

_acl_permissions() {
  local path="$1" key="$2"
  getfacl -cp "$path" 2>/dev/null | awk -F: -v key="$key" '
    $1 ":" $2 == key {
      sub(/[[:space:]].*/, "", $3)
      print $3
      exit
    }'
}

_acl_add_execute() {
  local permissions="$1"
  case "$permissions" in
    ---) printf '%s\n' "--x" ;;
    r--) printf '%s\n' "r-x" ;;
    -w-) printf '%s\n' "-wx" ;;
    rw-) printf '%s\n' "rwx" ;;
    r-x|-wx|rwx) printf '%s\n' "$permissions" ;;
    *) printf '%s\n' "--x" ;;
  esac
}

ensure_group_traverse_acl() {
  local config_root="$1"
  local allowed_group="$2"
  local sudo_spec="${ORCHESTRA_ACL_SUDO-sudo}"
  local -a privilege=()
  local entry mask current_mask new_mask other owner_group

  [[ -d "$config_root" && ! -L "$config_root" ]] || return 0
  [[ -n "$allowed_group" ]] || return 1
  getent group "$allowed_group" >/dev/null 2>&1 \
    || { echo "ERROR: group does not exist: $allowed_group" >&2; return 1; }
  command -v setfacl >/dev/null 2>&1 \
    || { echo "ERROR: setfacl is required to grant group traverse access" >&2; return 1; }
  command -v getfacl >/dev/null 2>&1 \
    || { echo "ERROR: getfacl is required to verify group traverse access" >&2; return 1; }

  other="$(_acl_permissions "$config_root" "other:")"
  owner_group="$(_acl_permissions "$config_root" "group:")"
  [[ "$other" == "---" && "$owner_group" == "---" ]] \
    || { echo "ERROR: $config_root owning group or other access is not private" >&2; return 1; }

  if [[ -n "$sudo_spec" ]]; then
    read -r -a privilege <<< "$sudo_spec"
  fi

  "${privilege[@]}" setfacl -m "g:$allowed_group:--x" "$config_root" || return 1
  entry="$(_acl_permissions "$config_root" "group:$allowed_group")"
  mask="$(_acl_permissions "$config_root" "mask:")"
  if [[ "$entry" != "--x" || "$mask" != *x* ]]; then
    current_mask="$mask"
    [[ -n "$current_mask" ]] || current_mask="---"
    new_mask="$(_acl_add_execute "$current_mask")"
    "${privilege[@]}" setfacl -m "m::$new_mask" "$config_root" || return 1
  fi

  entry="$(_acl_permissions "$config_root" "group:$allowed_group")"
  mask="$(_acl_permissions "$config_root" "mask:")"
  other="$(_acl_permissions "$config_root" "other:")"
  [[ "$entry" == "--x" && "$mask" == *x* && "$other" == "---" ]] \
    || { echo "ERROR: group traverse ACL is not effective on $config_root" >&2; return 1; }
  return 0
}

ensure_broker_traverse_acl() {
  local config_root="$1"
  local broker_user="${ORCHESTRA_BROKER_USER:-orchestra-github}"
  local sudo_spec="${ORCHESTRA_ACL_SUDO-sudo}"
  local -a privilege=()
  local entry mask current_mask new_mask other owner_group

  [[ -d "$config_root" && ! -L "$config_root" ]] || return 0
  # This helper only repairs an existing broker deployment.  It never creates
  # the account, the broker directory or configuration.
  id "$broker_user" >/dev/null 2>&1 || return 0
  [[ -d "$config_root/github-dispatch" ]] || return 0

  command -v setfacl >/dev/null 2>&1 \
    || { echo "ERROR: setfacl is required to preserve broker traverse access" >&2; return 1; }
  command -v getfacl >/dev/null 2>&1 \
    || { echo "ERROR: getfacl is required to verify broker traverse access" >&2; return 1; }

  # Fail closed if the root has already been opened to other users.  Repairing
  # the broker entry must never be used to bless a broader permission change.
  other="$(_acl_permissions "$config_root" "other:")"
  owner_group="$(_acl_permissions "$config_root" "group:")"
  [[ "$other" == "---" && "$owner_group" == "---" ]] \
    || { echo "ERROR: $config_root owning group or other access is not private" >&2; return 1; }

  if [[ -n "$sudo_spec" ]]; then
    read -r -a privilege <<< "$sudo_spec"
  fi

  # Add only execute/traverse.  setfacl normally raises the mask to include the
  # named entry; the fallback below handles an explicitly restrictive mask
  # without reducing any existing read/write mask bits.
  "${privilege[@]}" setfacl -m "u:$broker_user:--x" "$config_root" || return 1
  entry="$(_acl_permissions "$config_root" "user:$broker_user")"
  mask="$(_acl_permissions "$config_root" "mask:")"
  if [[ "$entry" != "--x" || "$mask" != *x* ]]; then
    current_mask="$mask"
    [[ -n "$current_mask" ]] || current_mask="---"
    new_mask="$(_acl_add_execute "$current_mask")"
    "${privilege[@]}" setfacl -m "m::$new_mask" "$config_root" || return 1
  fi

  entry="$(_acl_permissions "$config_root" "user:$broker_user")"
  mask="$(_acl_permissions "$config_root" "mask:")"
  other="$(_acl_permissions "$config_root" "other:")"
  [[ "$entry" == "--x" && "$mask" == *x* && "$other" == "---" ]] \
    || { echo "ERROR: broker traverse ACL is not effective on $config_root" >&2; return 1; }
  return 0
}

#!/usr/bin/env bash
# ensure-models-network.sh — shared lifecycle for the cross-compose model net
#
# The model network deliberately has no Compose owner.  Both the platform and
# model projects declare it as an external network, so creation and topology
# read-back must happen through this helper only.
#
# Source this file from deployment/serve entrypoints and call
# ``ensure_models_network``.  The optional network argument exists solely for
# isolated tests; production callers use the default ``anila-models-net``.
set -euo pipefail

ANILA_MODELS_NETWORK_DEFAULT="anila-models-net"

_models_network_name() {
  local network="${1:-$ANILA_MODELS_NETWORK_DEFAULT}"
  [[ "$network" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || { echo "invalid Docker network name: $network" >&2; return 2; }
  printf '%s\n' "$network"
}

verify_models_network() {
  local network internal driver
  network="$(_models_network_name "${1:-$ANILA_MODELS_NETWORK_DEFAULT}")" || return

  internal="$(docker network inspect "$network" --format '{{.Internal}}' 2>/dev/null)" \
    || {
      echo "$network topology read-back failed; refusing to use an unverified network" >&2
      return 1
    }
  driver="$(docker network inspect "$network" --format '{{.Driver}}' 2>/dev/null)" \
    || {
      echo "$network topology read-back failed; refusing to use it" >&2
      return 1
    }
  if [[ "$internal" != "true" || "$driver" != "bridge" ]]; then
    echo "$network must be Docker internal bridge (Internal=true, Driver=bridge); actual Internal=${internal:-missing} Driver=${driver:-missing}" >&2
    echo "Refusing automatic removal or recreation of an existing network." >&2
    return 1
  fi
}

_models_network_is_listed() {
  # A successful list query is the proof-of-absence boundary.  In contrast,
  # ``docker network inspect`` uses a non-zero exit status for both a missing
  # network and daemon/API/permission failures, so it must not drive create.
  local network names listed
  network="$(_models_network_name "${1:-$ANILA_MODELS_NETWORK_DEFAULT}")" || return

  names="$(docker network ls --format '{{.Name}}')" || {
    echo "Docker network discovery failed; refusing to create $network" >&2
    return 2
  }
  while IFS= read -r listed; do
    [[ "$listed" == "$network" ]] && return 0
  done <<< "$names"
  return 1
}

ensure_models_network() {
  local network listed_status
  network="$(_models_network_name "${1:-$ANILA_MODELS_NETWORK_DEFAULT}")" || return

  if _models_network_is_listed "$network"; then
    verify_models_network "$network"
    return
  else
    listed_status=$?
  fi
  if [[ "$listed_status" -ne 1 ]]; then
    # A daemon/API/permission/transient failure is not proof that the network
    # is absent.  Fail closed without attempting a mutating create command.
    return "$listed_status"
  fi

  # Only a successful list query that omitted the exact name reaches create.
  # A create race is harmless: the subsequent read-back remains authoritative.
  if ! docker network create --driver bridge --internal "$network" >/dev/null; then
    verify_models_network "$network"
    return
  fi
  verify_models_network "$network"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-ensure}" in
    ensure)
      ensure_models_network "${2:-$ANILA_MODELS_NETWORK_DEFAULT}"
      ;;
    verify)
      verify_models_network "${2:-$ANILA_MODELS_NETWORK_DEFAULT}"
      ;;
    help|-h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      ;;
    *)
      echo "usage: $0 [ensure|verify] [network-name]" >&2
      exit 2
      ;;
  esac
fi

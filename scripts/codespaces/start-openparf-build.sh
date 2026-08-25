#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if ! command -v setsid >/dev/null 2>&1; then
  echo "setsid is required to detach the OpenPARF build" >&2
  exit 2
fi

attempt="${1:-attempt-0001}"
if [[ ! "${attempt}" =~ ^attempt-[0-9]{4}$ ]]; then
  echo "attempt must match attempt-NNNN: ${attempt}" >&2
  exit 2
fi

log_root="${repository_root}/build/logs/codespaces/openparf-build"
log_path="${log_root}/${attempt}.log"
pid_path="${log_root}/${attempt}.pid"
status_path="${log_root}/${attempt}.status"

for path in "${log_path}" "${pid_path}" "${status_path}"; do
  if [[ -e "${path}" ]]; then
    echo "refusing to overwrite existing control artifact: ${path}" >&2
    exit 2
  fi
done

mkdir -p "${log_root}"
nohup setsid bash -c '
  runner="$1"
  status_path="$2"
  set +e
  "$runner"
  result=$?
  printf "%s\n" "$result" >"$status_path"
  exit "$result"
' bash \
  "${repository_root}/scripts/codespaces/build-openparf.sh" \
  "${status_path}" \
  >"${log_path}" 2>&1 </dev/null &
job_pid=$!
printf '%s\n' "${job_pid}" >"${pid_path}"

cat <<EOF
Started detached root-built OpenPARF gate.
  PID:    ${job_pid}
  log:    ${log_path}
  status: ${status_path}

Completion is recorded as 0; a nonzero value is failure:
  cat ${status_path}
EOF

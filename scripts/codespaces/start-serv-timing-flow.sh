#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if ! command -v setsid >/dev/null 2>&1; then
  echo "setsid is required to detach the diagnostic from this terminal" >&2
  exit 2
fi

output_root="${1:-build/serv-timing-diagnostic-001}"
if [[ "${output_root}" = /* ]]; then
  absolute_output_root="${output_root}"
else
  absolute_output_root="${repository_root}/${output_root}"
fi
attempt_name="$(basename "${absolute_output_root}")"
log_root="${repository_root}/build/logs"
log_path="${log_root}/${attempt_name}.log"
pid_path="${log_root}/${attempt_name}.pid"
status_path="${log_root}/${attempt_name}.status"

if [[ -e "${absolute_output_root}" ]]; then
  echo "refusing to overwrite existing output: ${absolute_output_root}" >&2
  exit 2
fi
for path in "${log_path}" "${pid_path}" "${status_path}"; do
  if [[ -e "${path}" ]]; then
    echo "refusing to overwrite existing control artifact: ${path}" >&2
    exit 2
  fi
done

mkdir -p "${log_root}"
nohup setsid bash -c '
  runner="$1"
  output_root="$2"
  status_path="$3"
  "$runner" "$output_root"
  result=$?
  printf "%s\n" "$result" >"$status_path"
  exit "$result"
' bash \
  "${repository_root}/scripts/codespaces/run-serv-timing-flow.sh" \
  "${absolute_output_root}" \
  "${status_path}" \
  >"${log_path}" 2>&1 </dev/null &
job_pid=$!
printf '%s\n' "${job_pid}" >"${pid_path}"

cat <<EOF
Started detached SERV timing flow.
  PID:    ${job_pid}
  output: ${absolute_output_root}
  log:    ${log_path}
  status: ${status_path}

Monitor without stopping the job:
  tail -f ${log_path}

Completion is recorded as 0; a nonzero value is failure:
  cat ${status_path}
EOF

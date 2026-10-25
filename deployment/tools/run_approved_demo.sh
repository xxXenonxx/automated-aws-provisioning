#!/bin/bash
# Run by the user in the prepared EC2 controller's interactive terminal.
# Never supply the database password as a command argument or a saved file.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '%s\n' 'Run this script with bash; do not source it.' >&2
    return 2
fi
set +xv
set -euo pipefail
umask 077

if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    printf '%s\n' \
        'Usage: bash tools/run_approved_demo.sh [--limit MONTHLY_USD]' \
        'Run in the prepared EC2 controller terminal as ec2-user.' \
        'Validates local inputs, then asks for the database password twice.' \
        'Choose the monthly forecast ceiling after reviewing the calculated estimate.' \
        'No password is accepted through arguments or saved by this helper.'
    exit 0
fi
reviewed_monthly_limit=''
if [[ $# -eq 2 && "$1" == --limit ]]; then
    reviewed_monthly_limit="$2"
elif [[ $# -ne 0 ]]; then
    printf '%s\n' 'Use --limit MONTHLY_USD or run without arguments to enter the reviewed amount.' >&2
    exit 2
fi
if [[ "$(uname -s)" != Linux || ! -t 0 || ! -t 1 || ! -t 2 ]]; then
    printf '%s\n' 'Use an interactive Linux terminal on the prepared EC2 controller; piped input is rejected.' >&2
    exit 1
fi
# The reviewed controller is a standard AL2023 EC2 host, not a CloudShell or
# container execution environment. This check makes no AWS or metadata calls.
if [[ ! -r /sys/devices/virtual/dmi/id/sys_vendor ]] || \
    [[ "$(cat /sys/devices/virtual/dmi/id/sys_vendor)" != 'Amazon EC2' ]] || \
    [[ -n "${AWS_CONTAINER_CREDENTIALS_FULL_URI:-}${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}" ]]; then
    printf '%s\n' 'Run this on the prepared EC2 control node, not CloudShell or a container.' >&2
    exit 1
fi

unset RDS_MASTER_PASSWORD approved_demo_password approved_demo_confirmation
cleanup() {
    unset RDS_MASTER_PASSWORD approved_demo_password approved_demo_confirmation
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)"
cd "$REPO_DIR"
exec 9>"$REPO_DIR/.deployment.lock"
if ! flock -n 9; then
    printf '%s\n' 'Another deployment is using this project folder. Wait for it to finish.' >&2
    exit 1
fi
for required in config/environment.json config/cost-assumptions.json \
    tools/project_checks.py tools/cost_estimate.py validated-site.yml; do
    if [[ ! -f "$required" || ! -r "$required" ]]; then
        printf 'Missing readable project input: %s\n' "$required" >&2
        exit 1
    fi
done

if [[ -x /opt/aws-project-venv/bin/python && -x /opt/aws-project-venv/bin/ansible-playbook ]]; then
    CONTROLLER_VENV=/opt/aws-project-venv
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" && -x "$VIRTUAL_ENV/bin/ansible-playbook" ]]; then
    CONTROLLER_VENV="$VIRTUAL_ENV"
else
    printf '%s\n' 'The controller Ansible environment is unavailable. Activate its virtual environment first.' >&2
    exit 1
fi
PYTHON="$CONTROLLER_VENV/bin/python"
PLAYBOOK="$CONTROLLER_VENV/bin/ansible-playbook"
# Disable inherited debugging and Ansible file logging before any secret input.
export ANSIBLE_DEBUG=false ANSIBLE_VERBOSITY=0 ANSIBLE_DISPLAY_ARGS_TO_STDOUT=false
export ANSIBLE_LOG_PATH=/dev/null

printf '%s\n' 'Checking local deployment inputs before requesting the database password...'
"$PYTHON" tools/project_checks.py validate --config config/environment.json
"$PYTHON" tools/cost_estimate.py validate \
    --config config/environment.json --assumptions config/cost-assumptions.json
"$PLAYBOOK" validated-site.yml --syntax-check

if [[ -z "$reviewed_monthly_limit" ]]; then
    IFS= read -r -p 'After reviewing the estimate, enter your maximum monthly forecast in USD: ' reviewed_monthly_limit
fi
if [[ ! "$reviewed_monthly_limit" =~ ^[0-9]{1,9}([.][0-9]{1,2})?$ ]]; then
    printf '%s\n' 'Use a monthly USD amount such as 55.00.' >&2
    exit 1
fi

printf '%s\n' \
    'Valid matching password entries will start the approved live AWS deployment.' \
    "The workflow refreshes pricing and enforces your reviewed USD $reviewed_monthly_limit monthly forecast ceiling."
approved_demo_password=''
approved_demo_confirmation=''
if ! IFS= read -r -s -p 'Database password: ' approved_demo_password; then
    printf '\n%s\n' 'Password entry cancelled; deployment was not started.' >&2
    exit 1
fi
printf '\n'
if ! IFS= read -r -s -p 'Repeat database password to start the approved deployment: ' approved_demo_confirmation; then
    printf '\n%s\n' 'Confirmation cancelled; deployment was not started.' >&2
    exit 1
fi
printf '\n'
if [[ "$approved_demo_password" != "$approved_demo_confirmation" ]]; then
    printf '%s\n' 'The passwords do not match; deployment was not started.' >&2
    exit 1
fi
if [[ ${#approved_demo_password} -lt 8 || ${#approved_demo_password} -gt 41 || \
      "$approved_demo_password" == *'/'* || "$approved_demo_password" == *'"'* || \
      "$approved_demo_password" == *'@'* ]]; then
    printf '%s\n' 'Use 8-41 characters without slash, double quote or @; deployment was not started.' >&2
    exit 1
fi
unset approved_demo_confirmation

# A shell environment assignment reaches only this Ansible child. The password
# is not an argument, an extra variable, a printed message or a stored file.
RDS_MASTER_PASSWORD="$approved_demo_password" \
    "$PLAYBOOK" validated-site.yml -e "approved_monthly_estimate_limit_usd=$reviewed_monthly_limit"

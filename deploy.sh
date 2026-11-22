#!/bin/bash
# Run the project from the EC2 controller terminal.
# Database secrets are stored in the local Ansible Vault file.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    printf '%s\n' 'Run this script with bash; do not source it.' >&2
    return 2
fi
set +xv
set -euo pipefail
umask 077

if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    printf '%s\n' \
        'Usage: bash deploy.sh [--limit MONTHLY_USD]' \
        'Run in the prepared EC2 controller terminal as ec2-user.' \
        'Validates inputs and unlocks the encrypted database settings.' \
        'Choose the monthly forecast ceiling after reviewing the calculated estimate.' \
        'Database and Vault passwords are never command arguments.'
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
# Require an EC2 controller rather than CloudShell.
if [[ ! -r /sys/devices/virtual/dmi/id/sys_vendor ]] || \
    [[ "$(cat /sys/devices/virtual/dmi/id/sys_vendor)" != 'Amazon EC2' ]] || \
    [[ -n "${AWS_CONTAINER_CREDENTIALS_FULL_URI:-}${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}" ]]; then
    printf '%s\n' 'Run this on the prepared EC2 control node, not CloudShell or a container.' >&2
    exit 1
fi

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)"
cd "$REPO_DIR"
exec 9>"$REPO_DIR/.deployment.lock"
if ! flock -n 9; then
    printf '%s\n' 'Another deployment is using this project folder. Wait for it to finish.' >&2
    exit 1
fi
for required in config/environment.json config/cost-assumptions.json \
    tools/project_checks.py tools/cost_estimate.py site.yml; do
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

printf '%s\n' 'Checking the selected deployment inputs...'
"$PYTHON" tools/project_checks.py validate --config config/environment.json
"$PYTHON" tools/cost_estimate.py validate \
    --config config/environment.json --assumptions config/cost-assumptions.json

if [[ -z "$reviewed_monthly_limit" ]]; then
    IFS= read -r -p 'Approve estimate up to this USD amount (or type cancel): ' reviewed_monthly_limit
fi
if [[ "${reviewed_monthly_limit,,}" == cancel ]]; then
    printf '%s\n' 'Deployment cancelled before resource creation.'
    exit 0
fi
if [[ ! "$reviewed_monthly_limit" =~ ^[0-9]{1,9}([.][0-9]{1,2})?$ ]]; then
    printf '%s\n' 'Use a monthly USD amount such as 55.00.' >&2
    exit 1
fi

printf '%s\n' "The refreshed estimate must fit USD $reviewed_monthly_limit; this does not cap the later AWS bill."
"$PYTHON" tools/demo.py vault
"$PLAYBOOK" -i inventory/inventory.ini site.yml --vault-id project@prompt \
    -e @config/secrets.yml -e "approved_monthly_estimate_limit_usd=$reviewed_monthly_limit"

if ! "$PYTHON" tools/demo.py links; then
    printf '%s\n' 'Deployment finished, but report links were unavailable. Use the report download command or retry: python tools/demo.py links'
fi

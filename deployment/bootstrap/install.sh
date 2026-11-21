#!/bin/bash
# Install the controller tools on Amazon Linux 2023.
set -euo pipefail
umask 022

if [[ "${EUID}" -ne 0 ]]; then
    printf '%s\n' 'Run with sudo bash bootstrap/install.sh.' >&2
    exit 1
fi

if ! id ec2-user >/dev/null 2>&1; then
    printf '%s\n' 'Expected the ec2-user account from a standard AL2023 AMI.' >&2
    exit 1
fi

dnf install -y python3.11 python3.11-pip tar gzip git nano

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3.11 -m venv /opt/aws-project-venv
/opt/aws-project-venv/bin/python -m pip install --upgrade pip
/opt/aws-project-venv/bin/python -m pip install -r "$PROJECT_DIR/requirements.txt"

install -d -m 0750 -o ec2-user -g ec2-user /home/ec2-user/.ansible
if ! runuser -u ec2-user -- env HOME=/home/ec2-user \
    /opt/aws-project-venv/bin/ansible-galaxy collection install \
    --server https://galaxy.ansible.com -r "$PROJECT_DIR/collections.yml"; then
    printf '%s\n' 'Galaxy install failed; using the same 11.4.0 tag from the official Ansible repository.' >&2
    runuser -u ec2-user -- env HOME=/home/ec2-user GIT_TERMINAL_PROMPT=0 \
        /opt/aws-project-venv/bin/ansible-galaxy collection install \
        'git+https://github.com/ansible-collections/amazon.aws.git,11.4.0' --no-deps
fi

# Keep the browser terminal available.
if systemctl cat amazon-ssm-agent.service >/dev/null 2>&1; then
    systemctl enable --now amazon-ssm-agent.service
else
    printf '%s\n' 'SSM Agent is absent: install it before using Session Manager.' >&2
fi

/opt/aws-project-venv/bin/ansible --version
runuser -u ec2-user -- env HOME=/home/ec2-user \
    /opt/aws-project-venv/bin/ansible-galaxy collection list amazon.aws
printf '%s\n' \
    'Controller tools installed.' \
    'Activate /opt/aws-project-venv, then run python tools/demo.py start from deployment.'

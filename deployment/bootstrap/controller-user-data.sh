#!/bin/bash
# EC2 user data for a standard Amazon Linux 2023 AMI.
# Installs controller software only. Does not deploy project resources.
set -euo pipefail
umask 022

if [[ "${EUID}" -ne 0 ]]; then
    printf '%s\n' 'Run this bootstrap as root (EC2 user data runs as root).' >&2
    exit 1
fi

if ! id ec2-user >/dev/null 2>&1; then
    printf '%s\n' 'Expected the ec2-user account from a standard AL2023 AMI.' >&2
    exit 1
fi

dnf install -y python3.11 python3.11-pip tar gzip git

# Leave AL2023 system Python /usr/bin/python3 unchanged.
python3.11 -m venv /opt/aws-project-venv
/opt/aws-project-venv/bin/python -m pip install --upgrade pip
# Keep these bounds aligned with requirements-controller.txt.
/opt/aws-project-venv/bin/python -m pip install \
    'ansible-core>=2.19,<2.20' \
    'boto3>=1.39.0,<2' \
    'botocore>=1.39.0,<2'

install -d -m 0750 -o ec2-user -g ec2-user /home/ec2-user/aws-project
install -d -m 0750 -o ec2-user -g ec2-user /home/ec2-user/.ansible
if ! runuser -u ec2-user -- env HOME=/home/ec2-user \
    /opt/aws-project-venv/bin/ansible-galaxy collection install \
    --server https://galaxy.ansible.com amazon.aws:11.4.0; then
    printf '%s\n' 'Galaxy install failed; using the same 11.4.0 tag from the official Ansible repository.' >&2
    runuser -u ec2-user -- env HOME=/home/ec2-user GIT_TERMINAL_PROMPT=0 \
        /opt/aws-project-venv/bin/ansible-galaxy collection install \
        'git+https://github.com/ansible-collections/amazon.aws.git,11.4.0' --no-deps
fi

# Standard AL2023 AWS AMIs normally include the SSM Agent. Permissions and
# network access must be configured separately before Session Manager works.
if systemctl cat amazon-ssm-agent.service >/dev/null 2>&1; then
    systemctl enable --now amazon-ssm-agent.service
else
    printf '%s\n' 'SSM Agent is absent: install it before using Session Manager.' >&2
fi

/opt/aws-project-venv/bin/ansible --version
runuser -u ec2-user -- env HOME=/home/ec2-user \
    /opt/aws-project-venv/bin/ansible-galaxy collection list amazon.aws
printf '%s\n' \
    'Controller software bootstrap completed.' \
    'Upload the project under /home/ec2-user/aws-project, then activate /opt/aws-project-venv.' \
    'No project playbooks have run and no project resources have been created.'

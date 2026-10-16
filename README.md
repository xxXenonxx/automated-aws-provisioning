# Automated AWS Infrastructure Provisioning

## 1. Project Overview

This project automates the provisioning and cleanup of AWS infrastructure using Ansible playbooks. It reduces repetitive manual configuration and makes resource deployment more consistent and repeatable.

The project currently automates:

- **Security Groups:** Creates a security group for project resources.
- **Amazon S3:** Creates an S3 bucket with versioning, server-side encryption, and public access blocking.
- **Amazon EC2:** Launches an EC2 instance using the configured AMI, subnet, security group, and key pair.
- **Key Pair Management:** Creates an AWS EC2 key pair and saves its private key locally.
- **Cleanup:** Removes the project's EC2 instance, S3 bucket and its object versions, security group, and AWS key-pair registration.

## 2. Project Structure

```text
automated-aws-provisioning/
├── playbooks/
│   ├── security_groups.yml
│   ├── s3.yml
│   ├── ec2.yml
│   ├── key_pair.yml
│   └── cleanup.yml
├── inventory/
│   └── inventory.ini
├── group_vars/
├── site.yml
├── .gitignore
└── README.md
```

The `site.yml` file is the master playbook. It imports the security group, S3, and EC2 playbooks in sequence. The key-pair and cleanup playbooks are run separately.

## 3. Prerequisites

Before running the project, ensure that you have:

- An AWS account.
- AWS CLI installed and configured.
- Ansible installed.
- The `amazon.aws` Ansible collection installed.
- An IAM identity with sufficient permissions to manage the required AWS resources.
- Access to the Git repository.

This project uses AWS region `us-east-1` (US East, N. Virginia).

### Install the required Ansible collection

Run in your Ubuntu terminal:

```bash
ansible-galaxy collection install amazon.aws
```

Check the installation:

```bash
ansible-galaxy collection list
```

### Configure AWS credentials

Configure credentials using:

```bash
aws configure
```

Enter the access key, secret key, default region (`us-east-1`), and output format when prompted.

Verify that the credentials work:

```bash
aws sts get-caller-identity --no-cli-pager
```

**Security:** Never commit AWS credentials, access keys, secret keys, or private SSH keys to GitHub. Use an IAM identity with only the permissions required by the project.

## 4. Open the Project

Navigate to the repository directory:

```bash
cd ~/projects/automated-aws-provisioning-github
```

Open it in VS Code:

```bash
code .
```

Run the following commands from the repository root unless otherwise stated.

## 5. Validate the Playbooks

Before provisioning resources, check the YAML syntax:

```bash
ansible-playbook playbooks/key_pair.yml --syntax-check
ansible-playbook site.yml --syntax-check
ansible-playbook playbooks/cleanup.yml --syntax-check
```

Each command should complete without syntax errors.

## 6. Provision AWS Resources

Run the playbooks in the following order.

### Step 1: Create the EC2 key pair

```bash
ansible-playbook playbooks/key_pair.yml
```

This playbook creates or checks the AWS key-pair registration and saves the private key locally when AWS returns it.

The configured key-pair name is:

```text
automated-aws-project-key
```

The expected local private-key path is:

```text
~/.ssh/automated-aws-project-key.pem
```

The private key must remain outside the repository. AWS does not allow an existing private key to be downloaded again. If the AWS key pair already exists but its private key is unavailable locally, resolve that situation before launching EC2.

### Step 2: Run the master playbook

```bash
ansible-playbook site.yml
```

The master playbook executes:

1. `playbooks/security_groups.yml`
2. `playbooks/s3.yml`
3. `playbooks/ec2.yml`

The playbooks use the configured AWS credentials and region to manage the resources.

### Step 3: Verify the resources

Check EC2 instances:

```bash
aws ec2 describe-instances \
  --region us-east-1 \
  --filters Name=tag:Name,Values=ansible-project-ec2 \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]' \
  --output table \
  --no-cli-pager
```

Check the security group:

```bash
aws ec2 describe-security-groups \
  --region us-east-1 \
  --group-names ansible-project-test \
  --query 'SecurityGroups[].[GroupId,GroupName]' \
  --output table \
  --no-cli-pager
```

Check S3 buckets:

```bash
aws s3 ls
```

The S3 playbook generates a bucket name using the configured prefix, AWS account ID, region, and a random suffix.

To inspect bucket encryption or versioning, substitute the actual bucket name:

```bash
aws s3api get-bucket-versioning \
  --bucket YOUR_BUCKET_NAME \
  --region us-east-1

aws s3api get-bucket-encryption \
  --bucket YOUR_BUCKET_NAME \
  --region us-east-1
```

## 7. Run Individual Playbooks

Each resource playbook can be executed independently when needed:

```bash
ansible-playbook playbooks/security_groups.yml
ansible-playbook playbooks/s3.yml
ansible-playbook playbooks/ec2.yml
```

Run the key-pair playbook separately before EC2 provisioning if the required AWS key pair is not available.

The master playbook does not include key-pair creation or cleanup.

## 8. Clean Up AWS Resources

**Warning: Cleanup is destructive.** It terminates matching EC2 instances and permanently deletes matching project S3 buckets and their object versions. Back up any data you need before proceeding.

Run:

```bash
ansible-playbook playbooks/cleanup.yml
```

The cleanup playbook attempts to:

1. Find project EC2 instances by their `Name` tag and terminate them.
2. Find S3 buckets matching the configured project bucket prefix and delete them, including their object versions.
3. Delete the project security group.
4. Delete the AWS key-pair registration.

The local private-key file is not deleted.

Review the cleanup playbook's region, instance name, bucket prefix, security group name, and key-pair name before execution. The current configuration is intended for the project resources in `us-east-1` and the specified AWS account.

### Verify cleanup

Check for remaining project EC2 instances:

```bash
aws ec2 describe-instances \
  --region us-east-1 \
  --filters Name=tag:Name,Values=ansible-project-ec2 \
            Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]' \
  --output table \
  --no-cli-pager
```

Also check the S3 console, EC2 security groups, EBS volumes, Elastic IP addresses, and AWS Billing dashboard.

A successful Ansible recap does not by itself prove that every resource in the AWS account has been removed.

## 9. Understanding Ansible Output

Ansible reports the result of each task:

- `ok`: The task ran successfully without changing the resource.
- `changed`: Ansible created, updated, or removed a resource.
- `skipping`: The task's condition was not met, so it did not run.
- `failed`: The task encountered an error.
- `unreachable`: Ansible could not connect to the target.

Warnings about an absent inventory and implicit localhost can occur because these playbooks execute AWS API operations from the local machine using `hosts: localhost` and `connection: local`. They do not automatically indicate a failure.

## 10. Cost and Security Considerations

- EC2 instances can incur compute charges while running. Stopping an instance may still leave charges for EBS storage and other associated resources.
- S3 storage, requests, and retained object versions may incur charges.
- Security groups and EC2 key-pair registrations do not normally have direct recurring charges.
- Terminating an instance does not guarantee that every associated resource is deleted.
- Check all AWS regions and services for resources not managed by these playbooks.
- Review the AWS Billing dashboard for current charges and any remaining resources.
- Keep SSH access restricted to trusted source IP addresses.
- Do not expose private SSH keys or AWS credentials in source control.

## 11. Troubleshooting

### Access denied

If AWS returns `AccessDenied`, check the IAM permissions attached to the configured identity and add only the missing permissions required by the task.

### Key pair already exists

AWS cannot return the private key for an existing key pair. If the corresponding local private key is missing, create a new AWS key pair under a different name, save its private key securely, and update `ec2.yml` to use the new name.

### EC2 instance not found during cleanup

The cleanup playbook searches by the configured `Name` tag and region. If the termination task is skipped, verify the instance ID, region, AWS account, and resource tags before assuming the instance has been removed.

### S3 bucket deletion fails

A versioned bucket can contain object versions and delete markers even when its normal object listing appears empty. Inspect the error and remaining bucket contents before retrying.

### Playbook fails because of permissions

Check the failed task and the AWS error message. Confirm that the selected IAM identity has the necessary EC2, S3, and security group permissions.

## 12. Typical Workflow

For a normal provisioning test:

```bash
cd ~/projects/automated-aws-provisioning-github

ansible-playbook playbooks/key_pair.yml
ansible-playbook site.yml
```

Verify the resources in AWS, then clean up when finished:

```bash
ansible-playbook playbooks/cleanup.yml
```

Always verify the final resource state in AWS after cleanup.

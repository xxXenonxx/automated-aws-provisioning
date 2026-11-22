# Automated AWS Provisioning with Ansible

Choose a server count, sizes and database options in the terminal. The project estimates the cost, asks for approval, creates an AWS environment, installs a media gallery and checks the result.

## Start

Use an Amazon Linux 2023 x86 EC2 controller with the `ProjectControlRole` instance profile, a public IPv4 address and internet access. Its security group needs outbound access; Session Manager does not require an inbound SSH rule. Application servers use `ProjectAppRole`, with access to the selected private S3 bucket. Create these roles before deployment.

In the controller's Session Manager terminal, run each box separately:

```bash
sudo -iu ec2-user
```

```bash
sudo dnf install -y git
```

```bash
git clone -b ahmed/checked-aws-provisioning https://github.com/xxXenonxx/automated-aws-provisioning.git ~/aws-project-video
```

```bash
sudo bash ~/aws-project-video/bootstrap/install.sh
```

```bash
cd ~/aws-project-video
source /opt/aws-project-venv/bin/activate
```

```bash
python tools/demo.py start
```

Answer the questions and review the summary. Press Enter to keep a displayed value. After the forecast appears, enter the monthly estimate you accept, or `cancel`. On the first run, enter the database password twice, then choose a separate Ansible Vault password. The database settings are encrypted in config/secrets.yml. Deployment asks for the Vault password to unlock them. Use the existing database password when preparing secrets for an existing database.

The accepted amount checks the deployment forecast. It does not cap the later AWS bill or stop resources automatically.

## What runs

`tools/demo.py start` asks questions and saves the answers. `deploy.sh` collects approval, prepares the Vault file if needed and runs **`site.yml`** with a Vault password prompt. This is the single Ansible deployment entry point.

| Stage | Files actually used |
| --- | --- |
| Prepare the SSH key | `playbooks/key_pair.yml` |
| Check the request and calculate AWS prices | `playbooks/preflight.yml`, `playbooks/estimate_cost.yml` |
| Create the VPC, public/private subnets and routes | `playbooks/network.yml` |
| Create server access rules, storage, servers and MySQL | `playbooks/security_groups.yml`, `s3.yml`, `ec2.yml`, `rds.yml` |
| Save the actual resource IDs and addresses | `playbooks/outputs.yml` |
| Install Apache, PHP and the gallery on every server | `playbooks/configure.yml` |
| Compare the created setup with the request and generate the report | `playbooks/verify.yml` |
| Remove a reviewed environment | `playbooks/cleanup.yml` |

The controller calls AWS APIs through the `amazon.aws` Ansible collection. It uses SSH to configure the application servers. `inventory/inventory.ini` contains the controller; the application inventory is filled from the actual created instance IDs and addresses.

The gallery stores image files in S3 and their titles, descriptions and file references in MySQL. All application servers use the same database and bucket. Uploading through one server can therefore be viewed through another. Multiple servers demonstrate the requested initial capacity; load balancing, automatic scaling and application failover are not implemented.

## Settings

The questions create `config/environment.json` and `config/cost-assumptions.json` locally. Ansible Vault encrypts the database secret in `config/secrets.yml`; the encrypted file is also excluded from Git. Its Vault password is not saved by the project. You can also edit them before starting. The local settings, encrypted secrets, SSH private key and generated reports are not committed.

Basic questions cover server count/size, disk size, database size/storage and Multi-AZ. Optional questions cover usage estimates, resource names, the supported Amazon Linux image, network CIDRs, key settings and the application instance profile. Region follows the controller. Permissions must allow the selected resources. The installed application currently supports Apache/PHP with MySQL and S3.

For an existing deployment, opening reports is the default menu choice. Running again retains its infrastructure names. The program does not automatically shrink capacity, replace networks or migrate databases.

`config/environment.demo.json` contains starting values, not a fixed deployment. `tools/` contains the pricing, validation and reporting functions used by the workflow; users do not run every helper separately.

## Encrypted secrets

To prepare the Vault file separately, run:

```bash
python tools/demo.py vault
```

The CLI passes the hidden database value to `ansible-vault encrypt` through standard input. Plaintext secrets are not written to a controller settings file. Keep the Vault password separately; forgetting it requires recovering or recreating the secret. After unlocking it, Ansible supplies the value to RDS and the application's protected configuration file. Vault protects stored secrets, not an application while it is running.

After choosing a different database or environment, use `python tools/demo.py vault --replace` to confirm and create matching encrypted settings. Creating or replacing the file does not change AWS resources; deployment still requires approval.

Ansible can also configure on-premises servers over SSH or WinRM. That is a capability of Ansible; this project's resource-creation playbooks currently target AWS.

## Reports

After deployment, the terminal prints the application addresses and report links. The reports are saved locally in `reports/` and privately in the project's S3 bucket. Bookmark the printed AWS-console location. Quick-open links expire after up to one hour; the saved files remain until deleted.

Refresh the links on the controller:

```bash
python tools/demo.py links
```

The controller needs `s3:PutObject` and `s3:GetObject` on the chosen bucket's `reports/*`. To download reports through CloudShell, clone this branch there and run:

```bash
python3 ~/aws-project-video/tools/demo.py download
```

Then choose **Actions > Download file**, using the saved path printed by the command. Add `--cost-only` for the estimate before deployment. CloudShell needs boto3 and permission to read the controller through Systems Manager.

The final report checks selected resource settings and website responses. Full photo upload/edit/delete is demonstrated manually; it is not claimed as an automatic verification check.

## Cleanup

Download any reports or images you need first. Preview only:

```bash
python tools/demo.py cleanup
```

To delete the displayed environment:

```bash
python tools/demo.py cleanup --execute
```

Choose whether to keep a final database snapshot, approve deletion of every S3 file/version and type the environment name. Cancelling these questions deletes nothing. Ansible refreshes the resource list and refuses if it changed after the preview. Discovery also supports a partially completed deployment or cleanup.

Cleanup removes the application servers, database, bucket and application network. It keeps the controller, IAM roles, SSH key and local reports. A retained database snapshot and the controller can continue to incur charges. The controller role must allow deletion of the selected project resources. AWS dependency errors stop cleanup; review the error and rerun the preview before retrying.

## Updating an older checkout

Run `git pull --ff-only` from `~/aws-project-video`, activate the virtual environment and use the same start command. Existing ignored settings, state and reports from the former `deployment/` folder are copied automatically to the root layout. The old folder is retained as `.previous-layout/`. Conflicting files stop the update instead of overwriting a deployment record. Updating the code does not redeploy or delete AWS resources.

# Automated AWS Provisioning with Ansible

The project creates an application network, one or more EC2 application servers, a private MySQL database and a private S3 bucket. Ansible installs the Apache/PHP example application on each server. The workflow checks the request, estimates its monthly cost, asks for approval, deploys and generates HTML reports.

## Files to use

| File | Purpose |
| --- | --- |
| `deployment/bootstrap/install.sh` | Install Python, Ansible and the AWS collection on the controller. |
| `deployment/config/environment.demo.json` | Starting settings for the two-server demonstration. |
| `deployment/config/environment.json` | Your prepared settings; generated locally and excluded from Git. |
| `deployment/config/cost-assumptions.json` | Expected usage for the estimate; generated locally and excluded from Git. |
| `deployment/tools/demo.py` | Short commands to Prompt for settings, estimate cost and view/download reports. |
| `deployment/deploy.sh` | Ask for approval/password and run the deployment. |
| `deployment/deploy.yml` | Ansible workflow: checks, cost, network/resources, software and final checks. |
| `Complete project/playbooks/provision.yml` | Create the network and resources. |
| `Complete project/playbooks/configure.yml` | Install the application. |
| `Complete project/playbooks/cleanup.yml` | Cleanup using the saved deployment resource IDs. |

The remaining Python files in `deployment/tools` implement pricing, checks, resource ownership and reporting. Users do not run each file separately. The root `site.yml` and `playbooks/` contain the earlier resource-only workflow. Use `deployment/tools/demo.py start` for the complete application workflow.

## Controller setup

Launch one Amazon Linux 2023 x86 EC2 controller with a public IPv4 address and the `ProjectControlRole` instance profile. For the supplied demo use `t3.micro`, an 8 GiB gp3 disk and one dedicated security group with outbound access. Connect through Session Manager. The application servers use the separate `ProjectAppRole` instance profile, scoped to the chosen bucket. Both roles must already exist.

Run the user switch separately, then continue as `ec2-user`:

```bash
sudo -iu ec2-user
```

```bash
sudo dnf install -y git
```

```bash
git clone -b ahmed/checked-aws-provisioning \
  https://github.com/xxXenonxx/automated-aws-provisioning.git ~/aws-project-video
```

```bash
sudo bash ~/aws-project-video/deployment/bootstrap/install.sh
```

```bash
cd ~/aws-project-video/deployment
source /opt/aws-project-venv/bin/activate
```

## Configure and deploy

```bash
python tools/demo.py start
```

Answer the questions for server count/size, disk size and the MySQL database. Press Enter to keep the displayed value. Usage and advanced settings are optional. The command detects the controller details, shows a request summary and saves the settings after confirmation. It then calculates the cost and asks for an accepted USD estimate limit and the database password before deployment. Type `cancel` at the approval prompt to stop before creating application resources.

If a saved deployment exists, the opening menu offers reports, reviewing the settings/running again, or exit. Opening reports is the default. Existing infrastructure names are retained when reviewing a deployed environment. Capacity reductions, network replacements and database migrations are not performed automatically.

The application currently supports the Apache/PHP example with private MySQL and S3. Region follows the controller. Other names, sizes and images must be allowed by the controller IAM role. The prompts write `config/environment.json` and `config/cost-assumptions.json`; no manual file editing is required. Local settings and private keys stay outside Git.

For separate stages, the existing commands remain available:

```bash
python tools/demo.py prepare --servers 2
python tools/demo.py estimate
```

```bash
bash deploy.sh
```

The accepted amount checks the deployment forecast, not the later AWS bill. On a rerun use the database's existing password.

## Reports

After a successful deployment, the terminal prints browser links and the saved S3 report location. The HTML files remain stored under `reports/` in the private project bucket until deleted. The printed AWS-console location can be bookmarked; sign in to view or download the saved files there. Public access stays blocked. Anyone holding a link can view that report for up to one hour, or until the signing credentials expire. Keep the links private.

To refresh the links on the controller:

```bash
python tools/demo.py links
```

The controller role needs `s3:PutObject` and `s3:GetObject` on `arn:aws:s3:::YOUR_BUCKET/reports/*`. The application's image permissions do not need to change. Links are created after the bucket and reports exist; use the terminal estimate or download command before deployment.

### Download a local copy

The deployment writes `reports/cost-estimate.html` and `reports/report.html` on the controller. To download them through CloudShell, clone the same branch there and run:

```bash
python3 ~/aws-project-video/deployment/tools/demo.py download
```

For the estimate before deployment use `download --cost-only`. If more than one controller matches, add `--controller INSTANCE_ID`. If boto3 is unavailable in CloudShell, install it with `python3 -m pip install --user boto3`.

In CloudShell choose **Actions > Download file** and enter `/home/cloudshell-user/project-reports/cost-estimate.html` or `/home/cloudshell-user/project-reports/report.html`. The copy command only reads the generated HTML reports; it does not create a public report site.

## Cleanup

Keep the controller and its local `deployment/state/deployment.json` until the reports are downloaded and cleanup is reviewed. The complete cleanup playbook checks the exact saved resource IDs and asks for explicit confirmation. Database snapshot handling must be selected before deletion. No cleanup is performed by preparation, estimation or report download.

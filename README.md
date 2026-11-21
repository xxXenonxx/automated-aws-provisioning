# Automated AWS Provisioning with Ansible

The project creates an application network, one or more EC2 application servers, a private MySQL database and a private S3 bucket. Ansible installs the Apache/PHP example application on each server. The workflow checks the request, estimates its monthly cost, asks for approval, deploys and generates HTML reports.

## Files to use

| File | Purpose |
| --- | --- |
| `deployment/bootstrap/install.sh` | Install Python, Ansible and the AWS collection on the controller. |
| `deployment/config/environment.demo.json` | Starting settings for the two-server demonstration. |
| `deployment/config/environment.json` | Your prepared settings; generated locally and excluded from Git. |
| `deployment/config/cost-assumptions.json` | Expected usage for the estimate; generated locally and excluded from Git. |
| `deployment/tools/demo.py` | Short commands to prepare settings, estimate cost and copy reports. |
| `deployment/deploy.sh` | Ask for approval/password and run the deployment. |
| `deployment/deploy.yml` | Ansible workflow: checks, cost, network/resources, software and final checks. |
| `Complete project/playbooks/provision.yml` | Team network and resource creation. |
| `Complete project/playbooks/configure.yml` | Team application installation. |
| `Complete project/playbooks/cleanup.yml` | Cleanup using the saved deployment resource IDs. |

The remaining Python files in `deployment/tools` implement pricing, checks, resource ownership and reporting. Users do not run each file separately. The original root `site.yml` and `playbooks/` are retained for the earlier resource-only version; use `deployment/deploy.sh` for the complete application workflow.

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

## Prepare, estimate and deploy

```bash
python tools/demo.py prepare
```

Preparation uses the demo defaults, fills the controller address/security group, formats the bucket name with your AWS account ID and prepares the application SSH key. It creates no application infrastructure. Existing local settings are retained. Change only the application count with `python tools/demo.py prepare --servers 2`, or edit `config/environment.json` before estimating. Defaults and IAM permissions must match your account and region.

```bash
python tools/demo.py estimate
```

Review the combined monthly forecast, then start deployment:

```bash
bash deploy.sh
```

Enter the accepted monthly forecast limit and the database password twice at the hidden prompts. The workflow refreshes the forecast and stops before provisioning if it exceeds the approved amount. This limit is not an AWS billing cap. On a rerun, use the database's existing password.

The two-server demo creates `automated-aws-provisioning-video-web` and `automated-aws-provisioning-video-web-02`, alongside the separate controller. Both receive the same software and share the database/bucket. Each server has its own address and disk. This is an initial server-count choice, not automatic scaling or load balancing.

## Reports

The deployment writes `reports/cost-estimate.html` and `reports/report.html` on the controller. To download them through CloudShell, clone the same branch there and run:

```bash
python3 ~/aws-project-video/deployment/tools/demo.py download
```

For the estimate before deployment use `download --cost-only`. If more than one controller matches, add `--controller INSTANCE_ID`. If boto3 is unavailable in CloudShell, install it with `python3 -m pip install --user boto3`.

In CloudShell choose **Actions > Download file** and enter `/home/cloudshell-user/project-reports/cost-estimate.html` or `/home/cloudshell-user/project-reports/report.html`. The copy command only reads the generated HTML reports; it does not create a public report site.

## Cleanup

Keep the controller and its local `deployment/state/deployment.json` until the reports are downloaded and cleanup is reviewed. The complete cleanup playbook checks the exact saved resource IDs and asks for explicit confirmation. Database snapshot handling must be selected before deletion. No cleanup is performed by preparation, estimation or report download.

#!/usr/bin/env python3
"""Prepare a deployment, estimate its cost and open or download its reports."""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse

BASE = Path(__file__).resolve().parent.parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def migrate_local_files():
    """Keep existing settings and resource IDs when updating the folder layout."""
    old = BASE / "deployment"
    copies = []
    has_local_files = False
    for folder in ("config", "state", "reports"):
        for source in (old / folder).glob("*"):
            if not source.is_file() or source.suffix not in (".json", ".html"):
                continue
            if folder == "config" and source.name not in ("environment.json", "cost-assumptions.json"):
                continue
            has_local_files = True
            target = BASE / folder / source.name
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise RuntimeError("Both layouts contain different local files. Keep the matching settings and state together before continuing.")
            if not target.exists():
                copies.append((source, target))
    backup = BASE / ".previous-layout"
    if has_local_files and backup.exists():
        raise RuntimeError("A previous-layout backup already exists. Review the local deployment folders before updating.")
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
    if has_local_files:
        old.rename(backup)
        print("Existing settings, deployment record and reports copied to the project folder. Original files kept as a backup.")


def controller_id():
    base = "http://169.254.169.254/latest/"
    request = urllib.request.Request(base + "api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    token = urllib.request.urlopen(request, timeout=5).read().decode()
    request = urllib.request.Request(base + "meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token})
    return urllib.request.urlopen(request, timeout=5).read().decode()


def prepare(args, session, settings=None, assumptions=None):
    from project_checks import validate_config
    from cost_estimate import validate_inputs
    target = BASE / "config/environment.json"
    config = settings if settings is not None else read_json(
        target if target.exists() else BASE / "config/environment.demo.json")
    if config.get("profile") != "complete_application":
        raise RuntimeError("Use the complete_application settings for this demo.")
    if args.servers is not None:
        config["ec2_instance_count"] = args.servers
    account = session.client("sts").get_caller_identity()["Account"]
    config["s3_bucket_name"] = config["s3_bucket_name"].replace("{account_id}", account)
    ec2 = session.client("ec2")
    node = ec2.describe_instances(InstanceIds=[controller_id()])["Reservations"][0]["Instances"][0]
    if len(node["SecurityGroups"]) != 1 or not node.get("PublicIpAddress"):
        raise RuntimeError("Use one dedicated controller security group and a public IPv4 address.")
    key = Path(config["ssh_private_key_file"]).expanduser()
    config.update(ssh_private_key_file=str(key),
        ssh_source_cidr=node["PublicIpAddress"] + "/32",
        control_node_security_group_id=node["SecurityGroups"][0]["GroupId"])
    errors = validate_config(config)
    if errors:
        raise RuntimeError("; ".join(item["field"] + ": " + item["message"] for item in errors))
    usage = BASE / "config/cost-assumptions.json"
    values = assumptions if assumptions is not None else read_json(
        usage if usage.exists() else BASE / "config/cost-assumptions.example.json")
    validate_inputs(config, values)
    image = ec2.describe_images(ImageIds=[config["ami_id"]])["Images"]
    if len(image) != 1 or image[0]["State"] != "available" or image[0]["Architecture"] != "x86_64":
        raise RuntimeError("The selected application image must be available and use x86_64.")
    session.client("iam").get_instance_profile(InstanceProfileName=config["app_instance_profile_name"])
    found = ec2.describe_key_pairs(Filters=[{"Name": "key-name", "Values": [config["ec2_key_name"]]}])["KeyPairs"]
    if found and not key.is_file():
        raise RuntimeError("The AWS key pair exists but its private file is missing. Resolve it before deploying.")
    if not found and key.exists():
        raise RuntimeError("A local key file already exists. Review it before creating another key.")
    save_json(target, config)
    save_json(usage, values)
    print("Settings ready:", target)
    print("Application servers:", config["ec2_instance_count"], "| Size:", config["instance_type"])
    print("Database:", config["rds_identifier"], "| Bucket:", config["s3_bucket_name"])
    print("The application resources have not been created. Review the cost next.")


def ask(label, default, convert=str, minimum=None, maximum=None, choices=None):
    while True:
        answer = input(f"{label} [{default}]: ").strip()
        try:
            value = convert(str(default) if not answer else answer)
            if minimum is not None and value < minimum:
                raise ValueError()
            if maximum is not None and value > maximum:
                raise ValueError()
            if choices is not None and value not in choices:
                raise ValueError()
            return value
        except ValueError:
            bounds = f" from {minimum} to {maximum}" if minimum is not None else ""
            allowed = ": " + ", ".join(map(str, choices)) if choices else bounds
            print("Enter a valid value" + allowed + ".")


def yes_no(label, default=False):
    answer = ask(label + " (yes/no)", "yes" if default else "no",
                 convert=str.lower, choices=("yes", "no", "y", "n"))
    return answer in ("yes", "y")


def configure(session):
    from project_checks import validate_config
    from cost_estimate import validate_inputs
    from deployment_plan import server_names
    path = BASE / "config/environment.json"
    config = read_json(path if path.exists() else BASE / "config/environment.demo.json")
    path = BASE / "config/cost-assumptions.json"
    usage = read_json(path if path.exists() else BASE / "config/cost-assumptions.example.json")
    account = session.client("sts").get_caller_identity()["Account"]
    config["s3_bucket_name"] = config["s3_bucket_name"].replace("{account_id}", account)
    ec2 = session.client("ec2")
    node = ec2.describe_instances(InstanceIds=[controller_id()])["Reservations"][0]["Instances"][0]
    if not node.get("PublicIpAddress") or len(node["SecurityGroups"]) != 1:
        raise RuntimeError("Use a controller with a public address and one dedicated security group.")
    root_id = next(item["Ebs"]["VolumeId"] for item in node["BlockDeviceMappings"]
                   if item["DeviceName"] == node["RootDeviceName"])
    volume = ec2.describe_volumes(VolumeIds=[root_id])["Volumes"][0]
    controller_hours = usage.get("control_node", {}).get("monthly_hours", 730)
    usage["include_existing_control_node"] = True
    usage["control_node"] = {"instance_type": node["InstanceType"],
        "root_volume_gib": volume["Size"], "root_volume_type": volume["VolumeType"],
        "monthly_hours": controller_hours, "public_ipv4_count": 1}
    config.update(ssh_source_cidr=node["PublicIpAddress"] + "/32",
                  control_node_security_group_id=node["SecurityGroups"][0]["GroupId"])
    previous = read_json(BASE / "state/deployment.json") if (BASE / "state/deployment.json").exists() else None
    if previous:
        identity = ("project_name", "environment_name", "aws_region")
        if (any(config[field] != previous[field] for field in identity)
                or config["rds_identifier"] != previous["resources"]["rds_identifier"]
                or config["s3_bucket_name"] != previous["resources"]["s3_bucket_name"]):
            raise RuntimeError("The local settings do not match this folder's saved deployment.")
    print("Press Enter to keep each suggested value. Ctrl+C cancels.")
    print("Application: Apache/PHP example. Database: MySQL. Region:", config["aws_region"])
    print("The controller address, disk and security group are detected automatically.")
    if previous:
        print("This folder already has a deployment. Names remain bound to it; no resources are deleted here.")
    while True:
        print("\nApplication servers")
        config["ec2_instance_count"] = ask("Number of application servers", config["ec2_instance_count"], int, 1, 20)
        config["instance_type"] = ask("Server size", config["instance_type"])
        usage["ec2_root_volume_gib"] = ask("Disk per application server (GiB)", usage["ec2_root_volume_gib"], int, 1, 65536)
        print("\nMySQL database")
        config["db_instance_class"] = ask("Database size", config["db_instance_class"])
        config["db_allocated_storage"] = ask("Database storage (GiB)", config["db_allocated_storage"], int, 20, 65536)
        config["db_multi_az"] = yes_no("Use a standby database (Multi-AZ)", config["db_multi_az"])
        if yes_no("Change the monthly usage assumptions for pricing"):
            usage["monthly_hours"] = ask("Application/database running hours", usage["monthly_hours"], float, 0, 744)
            usage["s3_standard_storage_gb"] = ask("Expected S3 storage (GB)", usage["s3_standard_storage_gb"], float, 0, 100000000)
            usage["s3_put_requests"] = ask("Expected S3 write requests", usage["s3_put_requests"], int, 0, 1000000000000)
            usage["s3_get_requests"] = ask("Expected S3 read requests", usage["s3_get_requests"], int, 0, 1000000000000)
        if yes_no("Change advanced names, network, image or access settings"):
            if not previous:
                config["project_name"] = ask("Project label", config["project_name"])
                config["environment_name"] = ask("Environment label", config["environment_name"])
                config["rds_identifier"] = ask("Database resource name", config["rds_identifier"])
                config["s3_bucket_name"] = ask("Private S3 bucket name", config["s3_bucket_name"])
            else:
                print("Existing project, environment, region, database and bucket names are retained.")
            default_name = config.get("instance_name", config["project_name"] + "-" + config["environment_name"] + "-web")
            config["instance_name"] = ask("Application server base name", default_name)
            config["ami_id"] = ask("Amazon Linux 2023 x86 image ID", config["ami_id"])
            usage["ec2_root_volume_type"] = ask("Disk type", usage["ec2_root_volume_type"], choices=("gp2", "gp3"))
            for field, label in (("vpc_cidr", "VPC address range"), ("public_subnet_cidr", "Public subnet range"),
                    ("private_subnet_a_cidr", "Private database subnet A range"),
                    ("private_subnet_b_cidr", "Private database subnet B range")):
                config[field] = ask(label, config[field])
            if not previous:
                config["db_name"] = ask("Database name inside MySQL", config["db_name"])
                config["db_username"] = ask("Database administrator username", config["db_username"])
            config["app_instance_profile_name"] = ask("Application IAM instance profile", config["app_instance_profile_name"])
            config["ec2_key_name"] = ask("Application connection key name", config["ec2_key_name"])
            config["ssh_private_key_file"] = ask("Private key file on controller", config["ssh_private_key_file"])
            config["ssh_username"] = ask("Application Linux username", config.get("ssh_username", "ec2-user"))
            usage["control_node"]["monthly_hours"] = ask("Controller running hours per month", controller_hours, float, 0, 744)
        errors = validate_config(config)
        try:
            validate_inputs(config, usage)
        except ValueError as error:
            errors.append({"field": "Cost settings", "message": str(error)})
        if errors:
            for item in errors:
                print(item["field"] + ": " + item["message"])
            if yes_no("Correct the settings", True):
                continue
            return None
        print("\nReview your request")
        print("Servers:", ", ".join(server_names(config)))
        print("Size:", config["instance_type"], "| Disk each:", usage["ec2_root_volume_gib"], "GiB", usage["ec2_root_volume_type"])
        print("MySQL:", config["db_instance_class"], "| Storage:", config["db_allocated_storage"], "GiB | Multi-AZ:", config["db_multi_az"])
        print("Database:", config["rds_identifier"], "| Bucket:", config["s3_bucket_name"])
        print("Running hours:", usage["monthly_hours"], "| Expected S3 storage:", usage["s3_standard_storage_gb"], "GB")
        print("AWS permissions must allow the selected sizes, names and image. Region follows this controller.")
        if yes_no("Save this request and calculate the cost", True):
            return config, usage
        if not yes_no("Change the answers", True):
            return None


def start(args, session):
    if not sys.stdin.isatty():
        raise RuntimeError("Run start in the controller's interactive terminal.")
    if (BASE / "state/deployment.json").exists():
        print("A saved deployment already exists.")
        print("1. Open reports\n2. Review settings and run deployment again\n3. Exit")
        choice = ask("Choose", "1", choices=("1", "2", "3"))
        if choice == "1":
            links(argparse.Namespace(minutes=60), session)
            return 0
        if choice == "3":
            return 0
    selected = configure(session)
    if selected is None:
        print("Cancelled. Settings were not saved and deployment was not started.")
        return 0
    prepare(argparse.Namespace(servers=None), session, settings=selected[0], assumptions=selected[1])
    result = estimate()
    if result != 0:
        return result
    print("Review the estimate above. Enter an accepted USD amount at the next prompt, or type cancel.")
    return subprocess.call(["bash", str(BASE / "deploy.sh")], cwd=BASE)


def estimate():
    playbook = Path("/opt/aws-project-venv/bin/ansible-playbook")
    if not playbook.is_file():
        raise RuntimeError("Install the controller tools with bootstrap/install.sh first.")
    return subprocess.call([str(playbook), "-i", "inventory/inventory.ini", "playbooks/estimate_cost.yml"], cwd=BASE)


def vault(replace=False):
    """Encrypt the database secret through the standard Ansible Vault CLI."""
    if not sys.stdin.isatty():
        raise RuntimeError("Create the secret in the controller's interactive terminal.")
    target = BASE / "config/secrets.yml"
    original = target.read_bytes() if target.exists() else None
    if original is not None and not replace:
        if not target.read_bytes().startswith(b"$ANSIBLE_VAULT;"):
            raise RuntimeError("config/secrets.yml must be encrypted with Ansible Vault before deployment.")
        print("Encrypted secrets already exist. Deployment will ask for the Vault password.")
        return 0
    if original is not None and not yes_no("Replace the saved database secret for the selected settings", False):
        print("Cancelled. The existing Vault file was kept.")
        return 0
    settings = read_json(BASE / "config/environment.json")
    secret = getpass.getpass("Database password: ")
    repeated = getpass.getpass("Repeat database password: ")
    if secret != repeated or not 8 <= len(secret) <= 41 or any(c in secret for c in '/"@'):
        raise RuntimeError("Use matching passwords of 8-41 characters without slash, double quote or @.")
    executable = Path("/opt/aws-project-venv/bin/ansible-vault")
    if not executable.is_file():
        raise RuntimeError("Install the controller tools before creating the Vault file.")
    values = {"project_database_password": secret, "project_secret_project": settings["project_name"],
              "project_secret_environment": settings["environment_name"],
              "project_secret_database": settings["rds_identifier"]}
    print("Choose a separate Vault password to encrypt this secret. Remember it; it is not saved.")
    temporary = target.with_suffix(".tmp")
    try:
        result = subprocess.run([str(executable), "encrypt", "--vault-id", "project@prompt",
                                 "--output", str(temporary), "-"],
                                input=json.dumps(values).encode("utf-8"), stdout=subprocess.PIPE, cwd=BASE)
        if result.returncode != 0:
            raise RuntimeError("Vault encryption did not finish. Deployment was not started.")
        if not temporary.read_bytes().startswith(b"$ANSIBLE_VAULT;"):
            raise RuntimeError("Vault did not return an encrypted file. Nothing was saved.")
        temporary.chmod(0o600)
        current = target.read_bytes() if target.exists() else None
        if current != original:
            raise RuntimeError("The secrets file changed during encryption. It was not replaced.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Encrypted database settings saved in config/secrets.yml.")
    return 0


def cleanup(args, session):
    from resource_ownership import cleanup_plan
    from project_checks import validate_config
    config = read_json(BASE / "config/environment.json")
    if validate_config(config):
        raise RuntimeError("Use valid saved settings before previewing cleanup.")
    account = session.client("sts").get_caller_identity()["Account"]
    clients = {name: session.client(name) for name in ("sts", "ec2", "rds", "s3")}
    plan = cleanup_plan(config, clients, account)
    print("Cleanup preview | Account:", account, "| Environment:", config["environment_name"])
    for name, value in plan["resources"].items():
        if value:
            print(name.replace("_", " ") + ":", ", ".join(value) if isinstance(value, list) else value)
    print("The controller, IAM roles and SSH key are kept. Download any reports you need first.")
    if not any(plan["resources"].values()):
        print("No application resources remain for this environment.")
        return 0
    if not args.execute:
        print("Nothing was deleted. To continue: python tools/demo.py cleanup --execute")
        return 0
    if not sys.stdin.isatty():
        raise RuntimeError("Approve cleanup in an interactive controller terminal.")
    import fcntl
    with (BASE / ".deployment.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment or cleanup is running. Wait until it finishes.") from None
        keep_snapshot = True
        if plan["resources"]["rds_identifier"]:
            keep_snapshot = yes_no("Keep a final database snapshot (AWS storage charges may continue)", True)
        if plan["resources"]["s3_bucket_name"] and not yes_no("Delete the bucket and ALL its files, versions and reports", False):
            print("Cancelled. Nothing was deleted.")
            return 0
        if input("Type the environment name to delete it: ").strip() != config["environment_name"]:
            print("Cancelled. Nothing was deleted.")
            return 0
        playbook = Path("/opt/aws-project-venv/bin/ansible-playbook")
        if not playbook.is_file():
            raise RuntimeError("Run cleanup on the prepared EC2 controller.")
        request = {"confirm_cleanup": True, "delete_bucket_contents": True,
                   "discard_database_snapshot": not keep_snapshot, "approved_cleanup_plan": plan,
                   "cleanup_snapshot_name": config["rds_identifier"] + "-final-" + time.strftime("%Y%m%d%H%M%S", time.gmtime())}
        (BASE / "state").mkdir(exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=BASE / "state", delete=False, encoding="utf-8") as stream:
            json.dump(request, stream)
            request_path = Path(stream.name)
        request_path.chmod(0o600)
        try:
            return subprocess.call([str(playbook), "-i", "inventory/inventory.ini", "playbooks/cleanup.yml",
                                    "-e", "@" + str(request_path)], cwd=BASE)
        finally:
            request_path.unlink(missing_ok=True)


def links(args, session):
    from project_checks import attach_cost_estimate, cost_estimate_html, report_html, validate_manifest
    config = read_json(BASE / "config/environment.json")
    manifest = read_json(BASE / "state/deployment.json")
    report = read_json(BASE / "reports/report.json")
    account = session.client("sts").get_caller_identity()["Account"]
    if validate_manifest(manifest) or manifest["account_id"] != account:
        raise RuntimeError("The saved deployment does not match this AWS account.")
    for field in ("project_name", "environment_name", "aws_region"):
        if manifest[field] != config[field] or report["deployment"][field] != manifest[field]:
            raise RuntimeError("The report does not match the selected deployment.")
    if (report.get("mode") != "live_read_only" or report["deployment"]["account_id"] != account
            or manifest["resources"]["s3_bucket_name"] != config["s3_bucket_name"]):
        raise RuntimeError("Use the live report and bucket belonging to this deployment.")
    for field in ("ec2_instance_ids", "vpc_id", "rds_identifier", "s3_bucket_name"):
        if report["resources"][field] != manifest["resources"][field]:
            raise RuntimeError("The report resource list differs from the saved deployment.")
    cost_path = BASE / "reports/cost-estimate.json"
    bound = attach_cost_estimate({}, manifest, cost_path)["cost_estimate"]
    if bound["status"] != "estimated" or bound["estimate"]["sample_only"]:
        raise RuntimeError("The cost report must match the live estimate used for deployment.")
    from botocore.config import Config
    s3 = session.client("s3", config=Config(signature_version="s3v4"))
    bucket = config["s3_bucket_name"]
    tags = s3.get_bucket_tagging(Bucket=bucket, ExpectedBucketOwner=account)["TagSet"]
    actual = {item["Key"]: item["Value"] for item in tags}
    if actual.get("Project") != config["project_name"] or actual.get("Environment") != config["environment_name"]:
        raise RuntimeError("The report bucket does not belong to the selected project.")
    pages = {"cost-estimate.html": cost_estimate_html(read_json(cost_path)),
             "report.html": report_html(report)}
    urls = []
    for name, html in pages.items():
        key = "reports/" + config["environment_name"] + "/" + name
        s3.put_object(Bucket=bucket, Key=key, Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8", ContentDisposition="inline",
            CacheControl="no-store", ServerSideEncryption="AES256", ExpectedBucketOwner=account)
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
            ExpiresIn=args.minutes * 60)
        urls.append((name, url))
    query = urllib.parse.urlencode({"region": config["aws_region"],
        "prefix": "reports/" + config["environment_name"] + "/", "showversions": "false"})
    print("Saved reports in AWS (bookmark this location; AWS sign-in required):")
    print("https://s3.console.aws.amazon.com/s3/buckets/" + bucket + "?" + query)
    print("The files remain saved until deleted. Only the quick-open links below expire.")
    print("Quick-open links last up to", args.minutes, "minutes, or until the signing credentials expire.")
    for name, url in urls:
        print(name + ":\n" + url)
    for url in manifest["resources"].get("website_urls", []):
        print("Application:", url)


def remote_read(ssm, instance_id, code):
    command = "sudo -u ec2-user /opt/aws-project-venv/bin/python -c " + shlex.quote(code)
    sent = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
        Comment="Download project HTML reports", Parameters={"commands": [command], "executionTimeout": ["30"]})
    for _ in range(90):
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(CommandId=sent["Command"]["CommandId"], InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        if result["Status"] != "Success" or result["ResponseCode"] != 0:
            raise RuntimeError("Report unavailable. Check that generation finished and the controller is online.")
        return result.get("StandardOutputContent", "").strip()
    raise RuntimeError("Report download timed out. Check the controller's Systems Manager connection.")


def copy_report(ssm, instance_id, directory, name, output):
    source = str(directory / "reports" / name)
    legacy = str(directory / "deployment" / "reports" / name)
    prefix = "from pathlib import Path; p=Path(" + repr(source) + "); b=(p if p.is_file() else Path(" + repr(legacy) + ")).read_bytes(); "
    size, digest = json.loads(remote_read(ssm, instance_id, prefix +
        "import hashlib,json; print(json.dumps([len(b),hashlib.sha256(b).hexdigest()]))"))
    if type(size) is not int or not 0 < size <= 2_000_000:
        raise RuntimeError("Unexpected report size. No downloaded file was replaced.")
    data = bytearray()
    for start in range(0, size, 10000):
        expression = "b[" + str(start) + ":" + str(start + 10000) + "]"
        encoded = remote_read(ssm, instance_id, prefix +
            "import base64; print(base64.b64encode(" + expression + ").decode())")
        data.extend(base64.b64decode(encoded, validate=True))
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("The report changed during download. Download again after generation finishes.")
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / (name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(output / name)
    print("Saved:", output / name)


def download(args, session):
    account = session.client("sts").get_caller_identity()["Account"]
    ec2 = session.client("ec2")
    if args.controller:
        if not re.fullmatch(r"i-[0-9a-f]{8,17}", args.controller):
            raise RuntimeError("Use a valid controller instance ID.")
        result = ec2.describe_instances(InstanceIds=[args.controller])
    else:
        result = ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": ["ansible-control-node"]},
            {"Name": "instance-state-name", "Values": ["running"]}])
    profile = f"arn:aws:iam::{account}:instance-profile/ProjectControlRole"
    nodes = [node for group in result["Reservations"] for node in group["Instances"]
        if node["State"]["Name"] == "running" and node.get("IamInstanceProfile", {}).get("Arn") == profile]
    if len(nodes) != 1:
        raise RuntimeError("Select one running ProjectControlRole controller with --controller INSTANCE_ID.")
    print("Controller:", nodes[0]["InstanceId"])
    directory = PurePosixPath("/home/ec2-user/aws-project-video")
    names = ["cost-estimate.html"] if args.cost_only else ["cost-estimate.html", "report.html"]
    for name in names:
        copy_report(session.client("ssm"), nodes[0]["InstanceId"], directory, name, Path.home() / "project-reports")
    print("In CloudShell choose Actions > Download file and enter a saved path.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Answer questions, review the estimate and approve deployment.")
    setup = sub.add_parser("prepare", help="Save settings without creating application resources.")
    setup.add_argument("--servers", type=int, choices=range(1, 21), metavar="1-20")
    sub.add_parser("estimate", help="Generate the live AWS cost forecast on the controller.")
    secret_setup = sub.add_parser("vault", help="Create the encrypted database settings through Ansible Vault.")
    secret_setup.add_argument("--replace", action="store_true", help="Confirm before replacing an existing Vault file.")
    remove = sub.add_parser("cleanup", help="Preview this environment's resources without deleting them.")
    remove.add_argument("--execute", action="store_true", help="Ask for confirmation, then run cleanup.")
    pages = sub.add_parser("links", help="Upload HTML reports privately and print temporary browser links.")
    pages.add_argument("--minutes", type=int, choices=range(1, 61), default=60, metavar="1-60")
    files = sub.add_parser("download", help="Copy generated HTML reports into CloudShell for download.")
    files.add_argument("--controller", help="Controller instance ID if automatic selection is ambiguous.")
    files.add_argument("--cost-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        migrate_local_files()
        if args.command == "estimate":
            return estimate()
        if args.command == "vault":
            return vault(args.replace)
        import boto3
        region = args.region
        if args.command in ("prepare", "links", "start", "cleanup"):
            path = BASE / "config/environment.json"
            selected = read_json(path if path.exists() else BASE / "config/environment.demo.json")
            region = selected["aws_region"]
        session = boto3.Session(region_name=region)
        if args.command == "start":
            return start(args, session)
        if args.command == "cleanup":
            return cleanup(args, session)
        if args.command == "prepare":
            prepare(args, session)
        elif args.command == "links":
            links(args, session)
        else:
            download(args, session)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. If deployment already began, check AWS for any created resources.", file=sys.stderr)
        return 130
    except ImportError:
        print("Install boto3 first: python3 -m pip install --user boto3", file=sys.stderr)
    except Exception as error:
        if isinstance(error, (RuntimeError, FileNotFoundError, ValueError)):
            message = str(error)
        elif isinstance(getattr(error, "response", None), dict):
            message = "AWS " + str(error.response.get("Error", {}).get("Code", "error"))
            message += " during " + str(getattr(error, "operation_name", "the request"))
            message += ". Check the controller permissions for your selected settings."
        else:
            message = "The step stopped. Check the controller connection, selected settings and AWS permissions."
        print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

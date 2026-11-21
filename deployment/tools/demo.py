#!/usr/bin/env python3
"""Prepare a deployment, estimate its cost and open or download its reports."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import time
import urllib.request

BASE = Path(__file__).resolve().parent.parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def controller_id():
    base = "http://169.254.169.254/latest/"
    request = urllib.request.Request(base + "api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    token = urllib.request.urlopen(request, timeout=5).read().decode()
    request = urllib.request.Request(base + "meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token})
    return urllib.request.urlopen(request, timeout=5).read().decode()


def prepare(args, session):
    from project_checks import validate_config
    target = BASE / "config/environment.json"
    config = read_json(target if target.exists() else BASE / "config/environment.demo.json")
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
    image = ec2.describe_images(ImageIds=[config["ami_id"]])["Images"]
    if len(image) != 1 or image[0]["State"] != "available" or image[0]["Architecture"] != "x86_64":
        raise RuntimeError("The selected application image must be available and use x86_64.")
    session.client("iam").get_instance_profile(InstanceProfileName=config["app_instance_profile_name"])
    found = ec2.describe_key_pairs(Filters=[{"Name": "key-name", "Values": [config["ec2_key_name"]]}])["KeyPairs"]
    if found and not key.is_file():
        raise RuntimeError("The AWS key pair exists but its private file is missing. Resolve it before deploying.")
    if not found:
        if key.exists():
            raise RuntimeError("A local key file already exists. Review it before creating another key.")
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = ec2.create_key_pair(KeyName=config["ec2_key_name"], KeyType="rsa", KeyFormat="pem")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result.pop("KeyMaterial"))
    key.chmod(0o600)
    save_json(target, config)
    usage = BASE / "config/cost-assumptions.json"
    if not usage.exists():
        save_json(usage, read_json(BASE / "config/cost-assumptions.example.json"))
    print("Settings ready:", target)
    print("Application servers:", config["ec2_instance_count"], "| Size:", config["instance_type"])
    print("Database:", config["rds_identifier"], "| Bucket:", config["s3_bucket_name"])
    print("The application resources have not been created. Review the cost next.")


def estimate():
    playbook = Path("/opt/aws-project-venv/bin/ansible-playbook")
    if not playbook.is_file():
        raise RuntimeError("Install the controller tools with bootstrap/install.sh first.")
    return subprocess.call([str(playbook), "-i", "localhost,", "playbooks/estimate_cost.yml"], cwd=BASE)


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
    print("Open these reports in your browser. Keep the links private.")
    print("Links last up to", args.minutes, "minutes, or until the signing credentials expire.")
    for name, url in urls:
        print(name + ":\n" + url)


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
    prefix = "from pathlib import Path, PurePosixPath; b=Path(" + repr(source) + ").read_bytes(); "
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
    directory = PurePosixPath("/home/ec2-user/aws-project-video/deployment")
    names = ["cost-estimate.html"] if args.cost_only else ["cost-estimate.html", "report.html"]
    for name in names:
        copy_report(session.client("ssm"), nodes[0]["InstanceId"], directory, name, Path.home() / "project-reports")
    print("In CloudShell choose Actions > Download file and enter a saved path.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("prepare", help="Prepare settings and the key on the EC2 controller.")
    setup.add_argument("--servers", type=int, choices=range(1, 21), metavar="1-20")
    sub.add_parser("estimate", help="Generate the live AWS cost forecast on the controller.")
    pages = sub.add_parser("links", help="Upload HTML reports privately and print temporary browser links.")
    pages.add_argument("--minutes", type=int, choices=range(1, 61), default=60, metavar="1-60")
    files = sub.add_parser("download", help="Copy generated HTML reports into CloudShell for download.")
    files.add_argument("--controller", help="Controller instance ID if automatic selection is ambiguous.")
    files.add_argument("--cost-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "estimate":
            return estimate()
        import boto3
        region = args.region
        if args.command in ("prepare", "links"):
            path = BASE / "config/environment.json"
            selected = read_json(path if path.exists() else BASE / "config/environment.demo.json")
            region = selected["aws_region"]
        session = boto3.Session(region_name=region)
        if args.command == "prepare":
            prepare(args, session)
        elif args.command == "links":
            links(args, session)
        else:
            download(args, session)
        return 0
    except ImportError:
        print("Install boto3 first: python3 -m pip install --user boto3", file=sys.stderr)
    except Exception as error:
        if isinstance(error, (RuntimeError, FileNotFoundError, json.JSONDecodeError)):
            message = str(error)
        else:
            message = "The step stopped. Check the controller connection, selected settings and AWS permissions."
        print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

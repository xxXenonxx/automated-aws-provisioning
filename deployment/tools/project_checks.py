#!/usr/bin/env python3
"""Offline input checks and scoped, read-only deployment verification.

No AWS SDK is imported until --live is explicitly selected. This module never
creates, changes, or deletes cloud resources. Reports intentionally exclude
passwords, credentials, raw SDK errors, and HTTP response bodies.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


REQUIRED_CONFIG = (
    "project_name", "environment_name", "aws_region", "ec2_key_name",
    "ssh_private_key_file", "ssh_source_cidr", "ami_id", "instance_type",
    "vpc_cidr", "public_subnet_cidr", "private_subnet_a_cidr",
    "private_subnet_b_cidr", "rds_identifier", "db_name", "db_username",
    "db_instance_class", "db_allocated_storage", "db_multi_az",
    "s3_bucket_name", "app_instance_profile_name",
)
OPTIONAL_CONFIG = {"ssh_username", "db_publicly_accessible"}
RESOURCE_CONFIG = (
    "profile", "project_name", "environment_name", "aws_region", "vpc_id",
    "subnet_id", "security_group_name", "instance_name", "ami_id",
    "instance_type", "ec2_key_name", "ssh_source_cidr", "s3_bucket_name",
    "rds_identifier", "db_name", "db_username", "db_instance_class",
    "db_allocated_storage", "db_subnet_group_name", "db_security_group_name",
    "control_node_security_group_id",
)
RESOURCE_KEYS = (
    "vpc_id", "ec2_instance_ids", "web_security_group_id",
    "db_security_group_id", "rds_identifier", "rds_endpoint", "rds_port",
    "s3_bucket_name", "website_url",
)
EXPECTED_KEYS = (
    "ssh_source_cidr", "db_publicly_accessible", "s3_public_access_block",
    "s3_encryption", "s3_versioning",
)
SECRET_ENV_NAMES = (
    "AWS_PROJECT_DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECURITY_TOKEN", "RDS_MASTER_PASSWORD",
)
MAX_JSON_BYTES = 1024 * 1024
MAX_COST_JSON_BYTES = 4 * 1024 * 1024


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _plain_string(value, maximum=255):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(ch) < 32 for ch in value))


def _bucket_valid(value):
    if not _matches(value, r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"):
        return False
    if ".." in value or ".-" in value or "-." in value:
        return False
    if value.startswith(("xn--", "sthree-", "amzn-s3-demo-")):
        return False
    if value.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3")):
        return False
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        return True


def _network(value):
    if not isinstance(value, str):
        raise ValueError("CIDR must be text")
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4:
        raise ValueError("IPv4 required")
    return network


def validate_config(config):
    """Return friendly errors without echoing configuration values.

    These are offline syntax and selected policy checks, not proof that an AMI,
    key, instance profile, quota, subnet placement, or AWS permission exists.
    """
    errors = []

    def error(field, message):
        errors.append({"field": field, "message": message})

    if not isinstance(config, dict):
        return [{"field": "config", "message": "Use one JSON object of settings."}]
    resource_only = config.get("profile") == "resource_provisioning"
    if "profile" in config and not resource_only:
        error("profile", "Use resource_provisioning for the root resource-only workflow, or omit profile for the complete application workflow.")
    required = RESOURCE_CONFIG if resource_only else REQUIRED_CONFIG
    allowed = set(RESOURCE_CONFIG) | {"ec2_instance_count"} if resource_only else set(REQUIRED_CONFIG) | OPTIONAL_CONFIG
    if set(config) - allowed:
        error("config", "Remove unsupported settings; secrets must not be stored in this file.")
    if resource_only and (type(config.get("ec2_instance_count", 1)) is not int or not 1 <= config.get("ec2_instance_count", 1) <= 20):
        error("ec2_instance_count", "Choose a whole number from 1 to 20 project servers. The controller is separate.")
    for key in required:
        if key not in config or config[key] is None or config[key] == "":
            error(key, "This setting is required.")

    patterns = {
        "project_name": (r"[A-Za-z][A-Za-z0-9_-]{0,39}", "Use a name starting with a letter, then letters, digits, underscores or hyphens (up to 40 characters)."),
        "environment_name": (r"[A-Za-z][A-Za-z0-9_-]{0,23}", "Use a short environment name starting with a letter (up to 24 characters)."),
        "aws_region": (r"[a-z]{2}(?:-[a-z]+)+-[0-9]", "Use an AWS region code, such as us-east-1."),
        "ec2_key_name": (r"[A-Za-z0-9_.@+=,-]{1,255}", "Use the name of an existing EC2 key pair, without spaces."),
        "ami_id": (r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", "Use an AMI identifier. Compatibility is checked separately against AWS."),
        "instance_type": (r"[a-z][a-z0-9-]*\.[a-z0-9]+", "Use an EC2 instance type, such as t3.micro."),
        "rds_identifier": (r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", "Use an RDS identifier starting with a letter, without a trailing hyphen (up to 63 characters)."),
        "db_name": (r"[A-Za-z][A-Za-z0-9]{0,63}", "Use a MySQL database name starting with a letter, followed by letters or digits."),
        "db_username": (r"[A-Za-z][A-Za-z0-9_]{0,15}", "Use a database username starting with a letter (up to 16 characters)."),
        "db_instance_class": (r"db\.[a-z][a-z0-9-]*\.[a-z0-9]+", "Use an RDS instance class, such as db.t3.micro."),
        "app_instance_profile_name": (r"[A-Za-z0-9+=,.@_-]{1,128}", "Use the name of a pre-existing EC2 instance profile for application access."),
    }
    if resource_only:
        patterns.update({
            "vpc_id": (r"vpc-(?:[0-9a-f]{8}|[0-9a-f]{17})", "Use the existing VPC identifier; this workflow does not create a VPC."),
            "subnet_id": (r"subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})", "Use an existing subnet identifier inside the selected VPC."),
            "control_node_security_group_id": (r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})", "Use the security-group identifier attached to the EC2 control node."),
            "security_group_name": (r"[A-Za-z0-9_.:/=+@-]{1,255}", "Use a name for the application's EC2 access group."),
            "db_security_group_name": (r"[A-Za-z0-9_.:/=+@-]{1,255}", "Use a name for the private database access group."),
            "db_subnet_group_name": (r"[A-Za-z0-9_.-]{1,255}", "Use a database subnet-group name containing letters, digits, underscores, dots or hyphens."),
            "instance_name": (r"[A-Za-z][A-Za-z0-9_.-]{0,127}", "Use a server name starting with a letter (up to 128 characters)."),
        })
    for key, (pattern, message) in patterns.items():
        if key in config and not _matches(config[key], pattern):
            error(key, message)
    if (resource_only and isinstance(config.get("security_group_name"), str)
            and isinstance(config.get("db_security_group_name"), str)
            and config["security_group_name"].casefold() == config["db_security_group_name"].casefold()):
        error("db_security_group_name", "Use different names for the EC2 and database access groups; otherwise database rules could replace server-access rules.")
    if isinstance(config.get("rds_identifier"), str) and "--" in config["rds_identifier"]:
        error("rds_identifier", "Do not use consecutive hyphens in the RDS identifier.")
    if "ssh_private_key_file" in config and not _plain_string(config["ssh_private_key_file"], 1024):
        error("ssh_private_key_file", "Provide the path to your private-key file; never paste its contents.")
    if "ssh_username" in config and not _matches(config["ssh_username"], r"[a-z_][a-z0-9_-]{0,31}"):
        error("ssh_username", "Use a Linux username such as ec2-user.")
    if "s3_bucket_name" in config and not _bucket_valid(config["s3_bucket_name"]):
        error("s3_bucket_name", "Use a valid lowercase S3 bucket name (3–63 characters); its availability is checked against AWS.")
    storage = config.get("db_allocated_storage")
    if "db_allocated_storage" in config and (type(storage) is not int or not 20 <= storage <= 65536):
        error("db_allocated_storage", "Use a whole number of GiB from 20 to 65536 for the supported MySQL setup.")
    if "db_multi_az" in config and type(config["db_multi_az"]) is not bool:
        error("db_multi_az", "Use the JSON value true or false.")
    if "db_publicly_accessible" in config and config["db_publicly_accessible"] is not False:
        error("db_publicly_accessible", "The supported deployment keeps the database private; use false.")

    networks = {}
    for key in ("vpc_cidr", "public_subnet_cidr", "private_subnet_a_cidr", "private_subnet_b_cidr"):
        if key not in config:
            continue
        try:
            network = _network(config[key])
            if not 16 <= network.prefixlen <= 28:
                error(key, "Use an IPv4 network between /16 and /28 for the supported VPC setup.")
            else:
                networks[key] = network
        except ValueError:
            error(key, "Use a valid IPv4 network in CIDR form, with no host bits, such as 10.20.1.0/24.")
    vpc = networks.get("vpc_cidr")
    subnet_keys = ("public_subnet_cidr", "private_subnet_a_cidr", "private_subnet_b_cidr")
    for key in subnet_keys:
        if vpc and key in networks and not networks[key].subnet_of(vpc):
            error(key, "This subnet must fit entirely inside the VPC network.")
    for index, key in enumerate(subnet_keys):
        for other in subnet_keys[index + 1:]:
            if key in networks and other in networks and networks[key].overlaps(networks[other]):
                error(key, "The public and private subnet networks must not overlap.")
    if "ssh_source_cidr" in config:
        try:
            ssh_net = _network(config["ssh_source_cidr"])
            if ssh_net.prefixlen < 24 or ssh_net.is_multicast or ssh_net.is_unspecified:
                error("ssh_source_cidr", "Restrict administrator access to your trusted IPv4 address (/32) or a small trusted range (/24 or narrower); do not open it to everyone.")
        except ValueError:
            error("ssh_source_cidr", "Provide a trusted IPv4 network in CIDR form, such as your public IP followed by /32.")
    return errors


def validate_manifest(manifest):
    errors = []
    if not isinstance(manifest, dict):
        return ["The manifest must be a JSON object."]
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        errors.append("Use manifest schema_version 1.")
    for key in ("project_name", "environment_name"):
        if not _plain_string(manifest.get(key), 100):
            errors.append("The manifest must include project and environment names.")
            break
    if not _matches(manifest.get("aws_region"), r"[a-z]{2}(?:-[a-z]+)+-[0-9]"):
        errors.append("The manifest needs a valid AWS region.")
    if not _matches(manifest.get("account_id"), r"[0-9]{12}"):
        errors.append("The manifest needs the 12-digit AWS account identifier.")
    resources = manifest.get("resources")
    expected = manifest.get("expected")
    if not isinstance(resources, dict) or not isinstance(expected, dict):
        return errors + ["The manifest needs resources and expected objects."]
    resource_only = expected.get("profile") == "resource_provisioning"
    if "ec2_count" in expected and (type(expected["ec2_count"]) is not int or not 1 <= expected["ec2_count"] <= 20):
        errors.append("The expected project-server count must be a whole number from 1 to 20.")
    if "profile" in expected and not resource_only:
        errors.append("The manifest contains an unsupported workflow profile.")
    if resource_only:
        if expected.get("application_configured") is not False:
            errors.append("The resource-only workflow must explicitly declare application_configured false.")
        if not _matches(expected.get("db_allowed_source_security_group_id"), r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})"):
            errors.append("The resource-only workflow needs its approved database source security-group identifier.")
    elif expected.get("application_configured") is False:
        errors.append("Skipping application checks requires the explicit resource_provisioning profile.")
    for key, pattern in (
        ("vpc_id", r"vpc-(?:[0-9a-f]{8}|[0-9a-f]{17})"),
        ("web_security_group_id", r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})"),
        ("db_security_group_id", r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})"),
        ("rds_identifier", r"[A-Za-z][A-Za-z0-9-]{0,62}"),
    ):
        if not _matches(resources.get(key), pattern):
            errors.append("The manifest has a missing or invalid resource identifier: " + key + ".")
    instance_ids = resources.get("ec2_instance_ids")
    if (not isinstance(instance_ids, list) or not 1 <= len(instance_ids) <= 20
            or len(set(str(item) for item in instance_ids)) != len(instance_ids)
            or any(not _matches(item, r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})") for item in instance_ids)):
        errors.append("Provide 1–20 distinct provisioned EC2 instance identifiers.")
    elif "ec2_count" in expected and len(instance_ids) != expected["ec2_count"]:
        errors.append("Recorded project-server identifiers must match the requested server count.")
    if not _bucket_valid(resources.get("s3_bucket_name")):
        errors.append("The manifest needs a valid S3 bucket name.")
    if not _plain_string(resources.get("rds_endpoint"), 255) or not _matches(resources.get("rds_endpoint"), r"[A-Za-z0-9.-]+"):
        errors.append("The manifest needs the database endpoint hostname, without credentials.")
    if type(resources.get("rds_port")) is not int or not 1 <= resources["rds_port"] <= 65535:
        errors.append("The manifest needs a valid database port.")
    if resource_only:
        if "website_url" not in resources or resources["website_url"] is not None:
            errors.append("The resource-only workflow must explicitly record website_url as null; no website is claimed.")
    else:
        try:
            url = urllib.parse.urlsplit(resources.get("website_url", ""))
            if (url.scheme not in ("http", "https") or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.port not in (None, 80, 443)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            errors.append("Use an HTTP(S) website URL without credentials, query parameters or a nonstandard port.")
    try:
        ssh_network = _network(expected.get("ssh_source_cidr"))
        if ssh_network.prefixlen < 24 or ssh_network.is_multicast or ssh_network.is_unspecified:
            raise ValueError()
    except ValueError:
        errors.append("The manifest needs an approved administrator CIDR (/24 or narrower).")
    if expected.get("db_publicly_accessible") is not False:
        errors.append("The approved database configuration must be private.")
    if expected.get("s3_public_access_block") is not True:
        errors.append("The approved bucket configuration must block public access.")
    if expected.get("s3_encryption") != "AES256" or expected.get("s3_versioning") != "Enabled":
        errors.append("The supported bucket baseline is AES256 encryption with versioning enabled.")
    return errors


def _safe_failure(exc):
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "403"}:
        return "AWS denied this read-only check. Review the control node's permissions."
    if code in {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "RequestExpired"}:
        return "AWS credentials are missing, expired or invalid. Refresh the control node's role or session."
    if code in {"NoSuchBucket", "DBInstanceNotFound", "InvalidInstanceID.NotFound", "InvalidGroup.NotFound", "404"}:
        return "The specified resource was not found or is not accessible in the selected account and region."
    return "This check could not finish. Check connectivity, AWS credentials and read permissions. No raw service error is included."


def _port_allows(permission, port):
    proto = str(permission.get("IpProtocol", ""))
    return proto == "-1" or (proto in {"tcp", "6"} and
                             permission.get("FromPort", 65536) <= port <= permission.get("ToPort", -1))


def _observed_ipv4(value):
    """Keep only a returned IPv4 address, never arbitrary response metadata."""
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(address) if address.version == 4 else None


def verify_manifest(manifest, clients, http_get, *, sample=False):
    """Inspect only IDs supplied by a validated manifest, using injected clients.

    Application-to-database/storage connectivity remains explicitly untested.
    An HTTP response alone is never presented as evidence of those connections.
    """
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Invalid deployment manifest.")
    resources, expected = manifest["resources"], manifest["expected"]
    resource_only = expected.get("profile") == "resource_provisioning"
    checks = []

    def add(check_id, status, message):
        checks.append({"id": check_id, "status": status, "message": message})

    def call(check_id, operation):
        try:
            return operation()
        except Exception as exc:
            add(check_id, "error", _safe_failure(exc))
            return None

    identity = call("aws_account", lambda: clients["sts"].get_caller_identity())
    if identity is None:
        add("deployment_checks", "not_checked", "Resource checks were not started because AWS account identity could not be established.")
        return _make_report(manifest, checks, sample)
    if identity.get("Account") != manifest["account_id"]:
        add("aws_account", "failed", "The active AWS account differs from the deployment manifest. No resources were inspected.")
        return _make_report(manifest, checks, sample)
    add("aws_account", "passed", "The active AWS account matches the deployment manifest.")

    ec2 = call("ec2_read", lambda: clients["ec2"].describe_instances(InstanceIds=resources["ec2_instance_ids"]))
    instances = []
    observed_addresses = []
    if ec2 is not None:
        instances = [item for reservation in ec2.get("Reservations", []) for item in reservation.get("Instances", [])]
        instances = [item for item in instances if item.get("InstanceId") in resources["ec2_instance_ids"]]
        # Addresses describe the current observation, not the approved baseline.
        # A normal public-IP change after restart must not become false drift.
        for instance_id in resources["ec2_instance_ids"]:
            item = next((entry for entry in instances if entry.get("InstanceId") == instance_id), None)
            if item is not None:
                observed_addresses.append({
                    "instance_id": instance_id,
                    "public_ipv4": _observed_ipv4(item.get("PublicIpAddress")),
                    "private_ipv4": _observed_ipv4(item.get("PrivateIpAddress")),
                })
        found = {item.get("InstanceId") for item in instances}
        for index, instance_id in enumerate(resources["ec2_instance_ids"], start=1):
            item = next((entry for entry in instances if entry.get("InstanceId") == instance_id), {})
            add("ec2_state_" + str(index), "passed" if item.get("State", {}).get("Name") == "running" else "failed",
                "The provisioned EC2 server is running." if item.get("State", {}).get("Name") == "running" else "A provisioned EC2 server is missing or is not running.")
        in_vpc = all(item.get("VpcId") == resources["vpc_id"] for item in instances) and len(found) == len(resources["ec2_instance_ids"])
        add("ec2_network", "passed" if in_vpc else "failed", "Provisioned EC2 servers match the expected VPC." if in_vpc else "A provisioned EC2 server does not match the expected VPC or was not returned.")
        correct_groups = bool(instances) and all({resources["web_security_group_id"]} == {group.get("GroupId") for group in item.get("SecurityGroups", [])} for item in instances)
        add("ec2_security_group", "passed" if correct_groups else "failed", "Each provisioned EC2 server has exactly its approved access group." if correct_groups else "A provisioned EC2 server's attached access groups differ from the approved setup.")
        if "ec2_count" in expected:
            count_ok = len(found) == expected["ec2_count"]
            add("ec2_count", "passed" if count_ok else "failed", f"Requested {expected['ec2_count']} project servers; AWS returned {len(found)}. The control node is separate.")
        if "ec2_instance_type" in expected:
            type_ok = bool(instances) and len(found) == len(resources["ec2_instance_ids"]) and all(item.get("InstanceType") == expected["ec2_instance_type"] for item in instances)
            add("ec2_instance_type", "passed" if type_ok else "failed", "All project servers use the selected instance type." if type_ok else "A project server's type differs from the selected type.")
        if "ec2_ami_id" in expected:
            image_ok = bool(instances) and len(found) == len(resources["ec2_instance_ids"]) and all(item.get("ImageId") == expected["ec2_ami_id"] for item in instances)
            add("ec2_image", "passed" if image_ok else "failed", "All project servers use the selected Linux image." if image_ok else "A project server's Linux image differs from the selected image.")

    groups = call("access_rules_read", lambda: clients["ec2"].describe_security_groups(GroupIds=[resources["web_security_group_id"], resources["db_security_group_id"]]))
    if groups is not None:
        group_map = {group.get("GroupId"): group for group in groups.get("SecurityGroups", [])}
        web = group_map.get(resources["web_security_group_id"])
        database = group_map.get(resources["db_security_group_id"])
        if web is None or database is None:
            add("access_groups", "failed", "One or both expected access groups were not returned.")
        else:
            permitted_ssh = [rule for rule in web.get("IpPermissions", []) if _port_allows(rule, 22)]
            actual_cidrs = {entry.get("CidrIp") for rule in permitted_ssh for entry in rule.get("IpRanges", [])}
            has_other_sources = any(rule.get("Ipv6Ranges") or rule.get("UserIdGroupPairs") or rule.get("PrefixListIds") for rule in permitted_ssh)
            ssh_ok = actual_cidrs == {expected["ssh_source_cidr"]} and not has_other_sources
            add("ssh_access_rules", "passed" if ssh_ok else "failed", "The web group's SSH sources match the approved administrator CIDR." if ssh_ok else "The web group's SSH sources differ from the approved administrator CIDR. Review the access rules.")
            db_rules = [rule for rule in database.get("IpPermissions", []) if _port_allows(rule, resources["rds_port"])]
            sources = {entry.get("GroupId") for rule in db_rules for entry in rule.get("UserIdGroupPairs", [])}
            db_open = any(rule.get("IpRanges") or rule.get("Ipv6Ranges") or rule.get("PrefixListIds") for rule in db_rules)
            approved_source = expected["db_allowed_source_security_group_id"] if resource_only else resources["web_security_group_id"]
            db_ok = sources == {approved_source} and not db_open
            approved_label = "control-node group" if resource_only else "web group"
            add("database_access_rules", "passed" if db_ok else "failed", "The database group's connection sources match the approved " + approved_label + "." if db_ok else "The database group's connection sources differ from the approved " + approved_label + ".")

    rds = call("rds_read", lambda: clients["rds"].describe_db_instances(DBInstanceIdentifier=resources["rds_identifier"]))
    if rds is not None:
        databases = [item for item in rds.get("DBInstances", []) if item.get("DBInstanceIdentifier") == resources["rds_identifier"]]
        database = databases[0] if databases else {}
        available = database.get("DBInstanceStatus") == "available"
        add("rds_ready", "passed" if available else "failed", "The database is available." if available else "The database is missing or is not yet available.")
        private = database.get("PubliclyAccessible") is False
        add("rds_private", "passed" if private else "failed", "The database is not publicly accessible." if private else "The database is public or its privacy setting could not be confirmed.")
        expected_group = resources["db_security_group_id"]
        attached_groups = {entry.get("VpcSecurityGroupId") for entry in database.get("VpcSecurityGroups", [])}
        group_ok = attached_groups == {expected_group}
        add("rds_security_group", "passed" if group_ok else "failed", "The database has exactly its approved access group." if group_ok else "The database's attached access groups differ from the approved setup.")
        endpoint_ok = (database.get("Endpoint", {}).get("Address") == resources["rds_endpoint"] and database.get("Endpoint", {}).get("Port") == resources["rds_port"])
        add("rds_endpoint", "passed" if endpoint_ok else "failed", "The manifest database address and port match AWS." if endpoint_ok else "The manifest database address or port differs from AWS.")

    bucket_args = {"Bucket": resources["s3_bucket_name"], "ExpectedBucketOwner": manifest["account_id"]}
    block = call("s3_public_access_read", lambda: clients["s3"].get_public_access_block(**bucket_args))
    if block is not None:
        settings = block.get("PublicAccessBlockConfiguration", {})
        protected = all(settings.get(key) is True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
        add("s3_public_access_block", "passed" if protected else "failed", "All four bucket public-access-block settings are enabled." if protected else "At least one required bucket public-access-block setting is not enabled.")
    encryption = call("s3_encryption_read", lambda: clients["s3"].get_bucket_encryption(**bucket_args))
    if encryption is not None:
        rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        encrypted = bool(rules) and all(rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == expected["s3_encryption"] for rule in rules)
        add("s3_encryption", "passed" if encrypted else "failed", "The bucket default encryption matches the approved AES256 setting." if encrypted else "The bucket default encryption differs from the approved AES256 setting.")
    versioning = call("s3_versioning_read", lambda: clients["s3"].get_bucket_versioning(**bucket_args))
    if versioning is not None:
        enabled = versioning.get("Status") == expected["s3_versioning"]
        add("s3_versioning", "passed" if enabled else "failed", "Bucket versioning is enabled." if enabled else "Bucket versioning is not enabled.")

    host = urllib.parse.urlsplit(resources["website_url"]).hostname if resources["website_url"] else None
    live_hosts = {str(item.get(key, "")).lower() for item in instances for key in ("PublicDnsName", "PublicIpAddress") if item.get(key)}
    if resource_only:
        add("website_http", "not_checked", "No application setup is declared for this resource-only workflow. No website response is claimed or requested.")
    elif not instances or any(item.get("State", {}).get("Name") != "running" for item in instances):
        add("website_http", "not_checked", "The HTTP check was not started because application-server readiness could not be confirmed.")
    elif host.lower() not in live_hosts:
        add("website_http", "failed", "The website host does not match a public address of the specified application servers. No HTTP request was sent.")
    else:
        try:
            status = http_get(resources["website_url"])
            responding = type(status) is int and 200 <= status < 300
            add("website_http", "passed" if responding else "failed", "The configured website returned an HTTP success response. This does not prove database or storage connectivity." if responding else "The configured website did not return an HTTP success response.")
        except Exception:
            add("website_http", "error", "The website could not be reached within the bounded HTTP check. No response body or raw network error is included.")
    if resource_only:
        add("application_database_connection", "not_checked", "No application setup is declared for this resource-only workflow. Database availability does not prove an application connection.")
        add("application_storage_connection", "not_checked", "No application setup is declared for this resource-only workflow. Bucket settings do not prove application file access.")
    else:
        add("application_database_connection", "not_checked", "Application-to-database connectivity was not tested. A running database and HTTP response do not prove this connection.")
        add("application_storage_connection", "not_checked", "Application-to-storage connectivity was not tested. Bucket settings do not prove the application's permissions or successful file access.")
    return _make_report(manifest, checks, sample, observed_addresses=observed_addresses)


def _redact(value):
    secrets = [os.environ.get(name) for name in SECRET_ENV_NAMES if os.environ.get(name)]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _make_report(manifest, checks, sample, *, observed_addresses=None):
    counts = {status: sum(check["status"] == status for check in checks) for status in ("passed", "failed", "not_checked", "error")}
    report = {
        "schema_version": 1,
        "mode": "sample" if sample else "live_read_only",
        "evidence_notice": ("SAMPLE — offline fixture data only. This report does not establish the state of any live AWS deployment." if sample else "Read-only observations at the recorded time. This is not a complete security audit or proof of application-data connectivity."),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "deployment": {**{key: manifest[key] for key in ("project_name", "environment_name", "aws_region", "account_id")},
                       "workflow": "Resource creation only; application setup is not declared" if manifest["expected"].get("profile") == "resource_provisioning" else "Complete application workflow"},
        "resources": {key: manifest["resources"][key] for key in RESOURCE_KEYS if key in manifest["resources"]},
        "summary": counts,
        "checked_scope_passed": counts["failed"] == 0 and counts["error"] == 0 and counts["passed"] > 0,
        "checks": checks,
        "limitations": [
            "Only the resources identified in this manifest and the checks listed here were inspected.",
            "Checks do not run continuously, apply corrections or test scaling. Any attached cost forecast is separate from resource verification and uses its stated assumptions.",
            "Application database and storage connections require separate authenticated application checks.",
            "Account identity, instance state and selected access settings do not establish that all resource settings are correct.",
        ],
    }
    if observed_addresses is not None:
        report["observed"] = {"ec2_addresses": observed_addresses}
    return _redact(report)


def _cost_text(value, maximum=512):
    if not _plain_string(value, maximum):
        raise ValueError("Invalid cost text")
    return value


def _cost_number(value):
    # Reject JSON booleans, NaN/Infinity and exponential values that would make
    # report formatting expensive. Keep the engine's decimal strings exact.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 64:
        raise ValueError("Invalid cost number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid cost number") from None
    if not number.is_finite() or not 0 <= number <= Decimal("1000000000000000000") or number.as_tuple().exponent < -30:
        raise ValueError("Invalid cost number")
    return format(number, "f")


def _cost_date(value):
    value = _cost_text(value, 64)
    date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Cost date requires timezone")
    return value


def sanitize_cost_estimate(value):
    """Allowlist a complete engine estimate; never copy arbitrary JSON metadata.

    Binding to a deployment is handled separately by attach_cost_estimate.
    This renderer also supports a standalone pre-deployment forecast.
    """
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("status") != "estimated" or value.get("currency") != "USD"
            or value.get("source") not in ("AWS_PRICE_LIST", "SAMPLE")
            or type(value.get("sample_only")) is not bool
            or value["sample_only"] != (value["source"] == "SAMPLE")):
        raise ValueError("Invalid estimate identity")
    if not _matches(value.get("region"), r"[a-z]{2}(?:-[a-z]+)+-[0-9]"):
        raise ValueError("Invalid cost region")
    result = {key: value[key] for key in ("schema_version", "status", "currency", "source", "sample_only", "region")}
    for key in ("project_name", "environment_name"):
        result[key] = _cost_text(value.get(key), 100)
    for key in ("config_sha256", "assumptions_sha256"):
        if key in value:
            if not _matches(value[key], r"[0-9a-f]{64}"):
                raise ValueError("Invalid cost input digest")
            result[key] = value[key]
    result["generated_at"] = _cost_date(value.get("generated_at"))
    # Use fixed labels and validated scalar values rather than exposing unknown
    # properties such as a password placed next to a cost assumption.
    assumptions = value.get("assumptions")
    if not isinstance(assumptions, dict):
        raise ValueError("Missing cost assumptions")
    result["assumptions"] = {}
    for key in ("monthly_hours", "ec2_root_volume_gib", "s3_standard_storage_gb", "s3_put_requests", "s3_get_requests"):
        result["assumptions"][key] = _cost_number(assumptions.get(key))
    if assumptions.get("ec2_root_volume_type") not in ("gp2", "gp3") or type(assumptions.get("include_existing_control_node")) is not bool:
        raise ValueError("Invalid disk or controller assumption")
    result["assumptions"].update({key: assumptions[key] for key in ("ec2_root_volume_type", "include_existing_control_node")})
    if assumptions["include_existing_control_node"]:
        node = assumptions.get("control_node")
        if not isinstance(node, dict) or not _matches(node.get("instance_type"), r"[a-z][a-z0-9-]*\.[a-z0-9]+") or node.get("root_volume_type") not in ("gp2", "gp3"):
            raise ValueError("Invalid controller assumptions")
        result["assumptions"]["control_node"] = {"instance_type": node["instance_type"], "root_volume_type": node["root_volume_type"], **{key: _cost_number(node.get(key)) for key in ("root_volume_gib", "monthly_hours", "public_ipv4_count")}}
    scope = value.get("scope")
    if not isinstance(scope, dict) or scope.get("profile") != "resource_provisioning" or type(scope.get("ec2_count")) is not int or not 1 <= scope["ec2_count"] <= 20 or scope.get("rds_multi_az") is not False or scope.get("rds_engine") != "MySQL" or scope.get("rds_storage_type") != "gp3":
        raise ValueError("Unsupported cost scope")
    result["scope"] = {key: scope[key] for key in ("profile", "ec2_count", "rds_multi_az", "rds_engine", "rds_storage_type")}
    for key, pattern in (("new_ec2_instance_type", r"[a-z][a-z0-9-]*\.[a-z0-9]+"), ("rds_instance_class", r"db\.[a-z][a-z0-9-]*\.[a-z0-9]+")):
        if not _matches(scope.get(key), pattern):
            raise ValueError("Invalid cost instance type")
        result["scope"][key] = scope[key]
    result["scope"]["rds_allocated_storage_gib"] = _cost_number(scope.get("rds_allocated_storage_gib"))
    lines = value.get("line_items")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 32:
        raise ValueError("Invalid cost line items")
    result["line_items"], seen = [], set()
    for line in lines:
        if not isinstance(line, dict) or not _matches(line.get("key"), r"[a-z][a-z0-9_]{0,63}") or line["key"] in seen or line.get("scope") not in ("new_resources", "existing_control_node") or line.get("unit") not in ("Hrs", "GB-Mo", "Requests"):
            raise ValueError("Invalid cost line")
        seen.add(line["key"])
        clean = {key: line[key] for key in ("key", "scope", "unit")}
        clean["label"] = _cost_text(line.get("label"))
        for key in ("quantity", "monthly_usd", "unrounded_monthly_usd"):
            clean[key] = _cost_number(line.get(key))
        clean["unit_price_usd"] = None if line.get("unit_price_usd") is None else _cost_number(line["unit_price_usd"])
        clean["effective_date"] = _cost_date(line.get("effective_date"))
        if line.get("publication_date") is not None:
            clean["publication_date"] = _cost_date(line["publication_date"])
        clean["sku"] = _cost_text(line.get("sku"), 100)
        tiers = line.get("tiers")
        if not isinstance(tiers, list) or len(tiers) > 32:
            raise ValueError("Invalid price tiers")
        clean["tiers"] = []
        for tier in tiers:
            if not isinstance(tier, dict):
                raise ValueError("Invalid price tier")
            clean["tiers"].append({key: _cost_number(tier.get(key)) for key in ("quantity", "unit_price_usd", "amount_usd")})
        quantity_sum = sum((Decimal(tier["quantity"]) for tier in clean["tiers"]), Decimal("0"))
        amount_sum = sum((Decimal(tier["amount_usd"]) for tier in clean["tiers"]), Decimal("0"))
        if quantity_sum != Decimal(clean["quantity"]) or amount_sum != Decimal(clean["unrounded_monthly_usd"]) or any(Decimal(tier["quantity"]) * Decimal(tier["unit_price_usd"]) != Decimal(tier["amount_usd"]) for tier in clean["tiers"]):
            raise ValueError("Inconsistent tier arithmetic")
        if Decimal(clean["monthly_usd"]) != amount_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise ValueError("Inconsistent line amount")
        result["line_items"].append(clean)
    expected_lines = {"ec2_compute", "ec2_disk", "ec2_ipv4", "rds_compute", "rds_storage", "s3_storage", "s3_put", "s3_get"}
    if assumptions["include_existing_control_node"]:
        expected_lines.update({"controller_compute", "controller_disk"})
        if Decimal(result["assumptions"]["control_node"]["public_ipv4_count"]):
            expected_lines.add("controller_ipv4")
    if seen != expected_lines or any((line["scope"] == "existing_control_node") != line["key"].startswith("controller_") for line in result["line_items"]):
        raise ValueError("Incomplete cost scope")
    totals = value.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("Missing cost totals")
    result["totals"] = {key: _cost_number(totals.get(key)) for key in ("new_resources_monthly_usd", "existing_control_node_monthly_usd", "combined_monthly_usd", "first_month_usd", "modeled_upfront_usd")}
    # An incomplete or hand-edited estimate must not quietly show a wrong total.
    subtotal = lambda scope: sum((Decimal(line["unrounded_monthly_usd"]) for line in result["line_items"] if line["scope"] == scope), Decimal("0"))
    new_total, controller_total = subtotal("new_resources"), subtotal("existing_control_node")
    for key, expected in (("new_resources_monthly_usd", new_total), ("existing_control_node_monthly_usd", controller_total), ("combined_monthly_usd", new_total + controller_total)):
        if Decimal(result["totals"][key]) != expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise ValueError("Inconsistent cost totals")
    if not assumptions["include_existing_control_node"] and controller_total:
        raise ValueError("Unexpected controller charge")
    if Decimal(result["totals"]["first_month_usd"]) != Decimal(result["totals"]["combined_monthly_usd"]) or Decimal(result["totals"]["modeled_upfront_usd"]) != 0:
        raise ValueError("Unsupported upfront costs")
    result["limitations"] = []
    if not isinstance(value.get("limitations"), list) or not 1 <= len(value["limitations"]) <= 24:
        raise ValueError("Invalid cost exclusions")
    for item in value["limitations"]:
        result["limitations"].append(_cost_text(item, 1600))
    result["sources"] = []
    if not isinstance(value.get("sources"), list) or not 1 <= len(value["sources"]) <= 24:
        raise ValueError("Invalid cost sources")
    for source in value["sources"]:
        source = _cost_text(source, 2048)
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme != "https" or parsed.hostname not in ("aws.amazon.com", "docs.aws.amazon.com") or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
            raise ValueError("Unsupported source URL")
        result["sources"].append(source)
    return _redact(result)


def attach_cost_estimate(report, manifest, path=None):
    """Attach only the exact forecast approved with the deployment manifest.

    Cost availability never changes the resource-verification check counts.
    The reason messages deliberately exclude raw file, JSON and SDK errors.
    """
    def unavailable(reason, status="unavailable"):
        report["cost_estimate"] = {"status": status, "message": reason, "totals": None}
        return report
    expected_digest = manifest.get("cost_estimate_sha256")
    if path is None:
        return unavailable("The manifest references a cost estimate, but no estimate file was loaded. Supply --cost-estimate with that exact file." if expected_digest else "No cost estimate was supplied. No estimated amount is available.", "unavailable" if expected_digest else "not_requested")
    if not _matches(expected_digest, r"[0-9a-f]{64}"):
        return unavailable("This manifest does not bind an approved estimate. Regenerate the manifest with the estimate's SHA-256 digest.")
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_COST_JSON_BYTES + 1)
        if len(data) > MAX_COST_JSON_BYTES:
            return unavailable("The estimate file exceeds the supported size limit.")
    except OSError:
        return unavailable("The estimate file could not be read. Supply the exact file recorded with this deployment.")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        return unavailable("The estimate file does not match the digest recorded with this deployment. No amount from this file is shown.")
    try:
        value = json.loads(data.decode("utf-8-sig"))
        if isinstance(value, dict) and value.get("status") == "error":
            return unavailable("The pricing step did not produce a complete estimate. Regenerate it after resolving the pricing-input or access problem; no zero-price fallback is used.")
        estimate = sanitize_cost_estimate(value)
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        return unavailable("The estimate is incomplete or unsupported. Regenerate it with the supported cost estimator; no amount is shown.")
    if any(estimate[key] != manifest[manifest_key] for key, manifest_key in (("project_name", "project_name"), ("environment_name", "environment_name"), ("region", "aws_region"))):
        return unavailable("The estimate belongs to a different project, environment or AWS Region. Regenerate it for this deployment.")
    if estimate["scope"]["ec2_count"] != len(manifest["resources"]["ec2_instance_ids"]):
        return unavailable("The estimate's project-server count does not match the recorded deployment. No amount is shown from a mismatched setup.")
    notice = ("SAMPLE COST ESTIMATE — demonstration prices only, even if resource checks use live AWS data." if estimate["sample_only"] else "ESTIMATE — AWS public list-price forecast, not an AWS bill or guaranteed charge.")
    report["cost_estimate"] = {"status": "estimated", "evidence_notice": notice, "sha256": expected_digest, "estimate": estimate}
    return report


def _report_document(title, content):
    """Keep the downloaded report self-contained, including the college crest."""
    esc = lambda value: html.escape(str(value), quote=True)
    try:
        crest_path = Path(__file__).resolve().parent.parent / "assets" / "asu-logo.png"
        logo = "data:image/png;base64," + base64.b64encode(crest_path.read_bytes()).decode("ascii")
        crest = "<div class='crest' role='img' aria-label='Faculty of Engineering, Ain Shams University'></div>"
    except OSError:
        logo = ""
        crest = "<div class='crest crest-fallback'>ASU<br>Engineering</div>"
    style = """
*{box-sizing:border-box}body{margin:0;background:#eee;color:#171717;font:16px/1.45 Calibri,Arial,sans-serif}
main{max-width:1040px;margin:24px auto;padding:34px 42px;background:#fff;border:3px double #292929}
header{display:flex;gap:20px;align-items:center;border-bottom:1px solid #777;padding-bottom:20px;margin-bottom:24px}
.crest{width:96px;height:96px;flex:none;background:url(__ASU_LOGO__) center/contain no-repeat}
.crest-fallback{display:flex;align-items:center;justify-content:center;text-align:center;font-weight:bold;color:#086491;border:1px solid #888}
.college{font-weight:bold;margin:0 0 4px}.project{margin:0;color:#444}h1{font:700 29px/1.2 'Times New Roman',serif;color:#c00000;margin:9px 0 0}
h2{font-size:22px;color:#c00000;margin:28px 0 10px}h3{font-size:18px;margin:22px 0 8px}
p{margin:9px 0}small,.subtle{color:#555}.notice,.outcome{padding:11px 14px;background:#f2f2f2;border-left:3px solid #777}
.notice.sample{background:#fff4d8;border-color:#9c6500;font-weight:bold}.total{font-size:25px;font-weight:bold;color:#c00000;margin:8px 0 14px}
table{border-collapse:collapse;width:100%;margin:12px 0 20px;table-layout:fixed}th,td{border:1px solid #aaa;padding:9px 11px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:#eee;font-weight:bold}.facts th{width:29%}.costs th:first-child{width:39%}.costs th:last-child{width:19%}
.checks th:first-child{width:25%}.checks th:nth-child(2){width:17%}td.passed{color:#17603d;font-weight:bold}td.failed,td.error{color:#a22127;font-weight:bold}td.not_checked{color:#795700;font-weight:bold}
li{margin:6px 0}footer{margin-top:28px;padding-top:10px;border-top:1px solid #aaa;color:#555;font-size:14px}
@media(max-width:680px){main{margin:0;padding:20px 16px;border-width:2px}header{gap:12px}.crest{width:72px;height:72px}h1{font-size:25px}table{font-size:14px}th,td{padding:7px}}
@media print{@page{size:A4;margin:14mm}body{background:#fff;font-size:11pt}main{max-width:none;margin:0;padding:18px;border:3px double #292929}header{padding-bottom:12px}.crest{width:72px;height:72px}h1{font-size:23pt}h2{font-size:16pt;break-after:avoid}h3{break-after:avoid}table{font-size:10pt}th,td{padding:6px 8px}thead{display:table-header-group}tr{break-inside:avoid}footer{font-size:9pt}}
""".replace("__ASU_LOGO__", logo)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + esc(title) + "</title><style>" + style + "</style></head><body><main>"
            "<header>" + crest +
            "<div><p class='college'>Ain Shams University - Faculty of Engineering</p>"
            "<p class='project'>Graduation Project | Automated AWS Infrastructure Provisioning</p>"
            "<h1>" + esc(title) + "</h1></div></header>" + content +
            "<footer>Automated AWS Infrastructure Provisioning - Graduation Project</footer></main></body></html>")


def _report_time(value):
    """Use the saved timestamp; never substitute the report opening time."""
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(dt.timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, TypeError, OverflowError):
        pass
    return str(value)


def _report_number(value):
    """Remove display-only trailing zeros without changing an amount."""
    text = str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def cost_estimate_section_html(cost):
    """Show the useful cost summary; detailed price evidence stays in JSON."""
    esc = lambda value: html.escape(str(value), quote=True)
    heading = "<h2>Estimated monthly cost</h2>"
    if cost.get("status") != "estimated":
        reason = cost.get("message", "")
        if "different project" in reason or "does not match the digest" in reason:
            message = "The saved estimate does not match this setup. Run the estimate again for this deployment."
        elif "not_requested" == cost.get("status") or reason.startswith("No cost estimate was supplied"):
            message = "No cost estimate was supplied for this setup."
        elif "could not be read" in reason or "no estimate file was loaded" in reason:
            message = "The saved estimate could not be opened. Select the estimate used for this deployment."
        else:
            message = "A complete matching estimate is not available. Resolve the cost check and run it again."
        return heading + "<p class='notice'>No estimate is available.</p><p>" + esc(message) + "</p>"
    estimate, totals = cost["estimate"], cost["estimate"]["totals"]
    scope, assumptions = estimate["scope"], estimate["assumptions"]
    groups = [
        ("Project servers, disks and public IPs", {"ec2_compute", "ec2_disk", "ec2_ipv4"}),
        ("MySQL database and storage", {"rds_compute", "rds_storage"}),
        ("S3 storage and requests", {"s3_storage", "s3_put", "s3_get"}),
    ]
    if assumptions["include_existing_control_node"]:
        groups.append(("EC2 control node, disk and public IP", {"controller_compute", "controller_disk", "controller_ipv4"}))
    rows = []
    for label, keys in groups:
        amount = sum((Decimal(line["unrounded_monthly_usd"]) for line in estimate["line_items"]
                      if line["key"] in keys), Decimal("0"))
        monthly = format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
        rows.append("<tr><td>" + esc(label) + "</td><td>USD " + monthly + "</td></tr>")
    facts = [
        ("Region", estimate["region"]),
        ("Hours running per month", _report_number(assumptions["monthly_hours"]) + " hours" +
         (" (about a full month)" if Decimal(assumptions["monthly_hours"]) == Decimal("730") else "")),
        ("Project servers", str(scope["ec2_count"]) + " Linux " + scope["new_ec2_instance_type"] + "; each with a " +
         _report_number(assumptions["ec2_root_volume_gib"]) + " GiB " + assumptions["ec2_root_volume_type"] + " disk"),
        ("Database", "MySQL " + scope["rds_instance_class"] + "; " +
         _report_number(scope["rds_allocated_storage_gib"]) + " GiB gp3 storage; runs in one Availability Zone"),
        ("Expected S3 storage", _report_number(assumptions["s3_standard_storage_gb"]) +
         " GB stored on average, including older file versions; this is expected usage, not reserved space"),
        ("Expected S3 requests per month", _report_number(assumptions["s3_put_requests"]) +
         " uploads or other writes; " + _report_number(assumptions["s3_get_requests"]) + " downloads or other reads"),
    ]
    if assumptions["include_existing_control_node"]:
        node = assumptions["control_node"]
        facts.append(("Control node", node["instance_type"] + "; " + _report_number(node["monthly_hours"]) +
                      " hours/month; " + _report_number(node["root_volume_gib"]) + " GiB " + node["root_volume_type"] +
                      " disk"))
        facts.append(("Public IP addresses", str(scope["ec2_count"]) + " for the project servers; " +
                      _report_number(node["public_ipv4_count"]) + " for the control node"))
        controller = "USD " + totals["existing_control_node_monthly_usd"]
    else:
        controller = "Not included in this estimate"
        facts.append(("Control node", controller))
        facts.append(("Public IP addresses", "One for the project server"))
    assumption_rows = "".join("<tr><th>" + esc(label) + "</th><td>" + esc(value) + "</td></tr>" for label, value in facts)
    notice = ("<p class='notice sample'>SAMPLE COST ESTIMATE - example prices only, not live AWS prices.</p>"
              if estimate["sample_only"] else "<p>Estimate based on AWS prices; actual charges depend on usage and applicable tax or credits.</p>")
    return (heading + notice + "<p class='total'>USD " + esc(totals["combined_monthly_usd"]) + " / month</p>"
            "<p class='subtle'>Estimated on " + esc(_report_time(estimate["generated_at"])) + ".</p>"
            "<table class='facts'><tr><th>Project resources</th><td>USD " + esc(totals["new_resources_monthly_usd"]) +
            " / month</td></tr><tr><th>Control node</th><td>" + esc(controller) + "</td></tr></table>"
            "<h3>Cost breakdown</h3><table class='facts'><thead><tr><th>Resource</th><th>Monthly estimate</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
            "<h3>Setup used for this estimate</h3><table class='facts'>" + assumption_rows + "</table>")


def cost_estimate_html(estimate):
    """Standalone forecast with the same calculation validation as before."""
    clean = sanitize_cost_estimate(estimate)
    section = cost_estimate_section_html({"status": "estimated", "estimate": clean})
    title = "Example Estimated Monthly Cost" if clean["sample_only"] else "Estimated Monthly Cost"
    esc = lambda value: html.escape(str(value), quote=True)
    context = "<p><b>Project:</b> " + esc(clean["project_name"]) + " &nbsp; <b>Environment:</b> " + esc(clean["environment_name"]) + "</p>"
    return _report_document(title, context + section)


def report_html(report):
    """Render readable setup details without changing recorded results."""
    esc = lambda value: html.escape(str(value), quote=True)
    sample = report["mode"] == "sample"
    title = "Example AWS Setup Report" if sample else "AWS Setup Report"
    notice = ("<p class='notice sample'>SAMPLE - offline example data only. This is not a live deployment.</p>"
              if sample else "<p class='subtle'>Checked on " + esc(_report_time(report["generated_at"])) + ".</p>")
    if report["checked_scope_passed"]:
        outcome = "All completed checks passed. Items marked Not checked still need separate checks."
    else:
        outcome = "Some checks failed or could not finish. Review the results before using the environment."
    deployment_labels = {
        "project_name": "Project", "environment_name": "Environment", "aws_region": "AWS region",
        "account_id": "AWS account", "workflow": "Deployment stage",
    }
    resource_labels = {
        "vpc_id": "Network (VPC)", "ec2_instance_ids": "Project EC2 server(s)",
        "web_security_group_id": "Server security group", "db_security_group_id": "Database security group",
        "rds_identifier": "Database name in AWS", "rds_endpoint": "Database address", "rds_port": "Database port",
        "s3_bucket_name": "S3 bucket", "website_url": "Website address",
    }
    metadata = []
    for key, label in deployment_labels.items():
        if key in report["deployment"]:
            value = report["deployment"][key]
            if key == "workflow" and value == "Resource creation only; application setup is not declared":
                value = "AWS resources created; application software is not installed in this stage"
            metadata.append("<tr><th>" + esc(label) + "</th><td>" + esc(value) + "</td></tr>")
    resources = []
    for key, label in resource_labels.items():
        if key in report["resources"]:
            value = report["resources"][key]
            value = ", ".join(value) if isinstance(value, list) else ("Not set up in this stage" if value is None else value)
            resources.append("<tr><th>" + esc(label) + "</th><td>" + esc(value) + "</td></tr>")
    address_section = ""
    address_rows = []
    for server in report.get("observed", {}).get("ec2_addresses", []):
        address_rows.append("<tr><td>" + esc(server["instance_id"]) + "</td><td>" +
                            esc(server.get("public_ipv4") or "Not assigned or unavailable") + "</td><td>" +
                            esc(server.get("private_ipv4") or "Not returned") + "</td></tr>")
    if address_rows:
        address_section = ("<h3>Current server addresses</h3>"
                           "<p>Addresses shown at the time of the checks. A public address can change after a stop and restart.</p>"
                           "<table class='facts'><thead><tr><th>EC2 server</th><th>Public IP address</th>"
                           "<th>Private IP address</th></tr></thead><tbody>" + "".join(address_rows) + "</tbody></table>")
    check_labels = {
        "aws_account": "Correct AWS account", "deployment_checks": "Resource checks", "ec2_read": "Read server details",
        "ec2_network": "Server network", "ec2_security_group": "Server security group",
        "ec2_count": "Requested project-server count", "ec2_instance_type": "Selected server type", "ec2_image": "Selected Linux image",
        "access_rules_read": "Read access rules", "access_groups": "Security groups",
        "ssh_access_rules": "SSH access", "database_access_rules": "Database access",
        "rds_read": "Read database details", "rds_ready": "Database available", "rds_private": "Database privacy",
        "rds_security_group": "Database security group", "rds_endpoint": "Database address and port",
        "s3_public_access_read": "Read S3 public access settings", "s3_public_access_block": "S3 public access blocked",
        "s3_encryption_read": "Read S3 encryption", "s3_encryption": "S3 encryption",
        "s3_versioning_read": "Read S3 versioning", "s3_versioning": "S3 file versioning",
        "website_http": "Website response", "application_database_connection": "Application database connection",
        "application_storage_connection": "Application storage connection",
    }
    statuses = {"passed": "Passed", "failed": "Failed", "not_checked": "Not checked", "error": "Could not finish"}
    rows = []
    for check in report["checks"]:
        label = check_labels.get(check["id"], "Resource check")
        if check["id"].startswith("ec2_state_"):
            label = "EC2 server running"
        message = check["message"]
        for old, new in (("deployment manifest", "saved setup"), ("manifest database", "saved database"),
                         ("manifest", "saved setup"), ("resource-only workflow", "resource creation stage"),
                         ("provisioned", "created"), ("administrator CIDR", "SSH address range"),
                         ("web group's", "server security group's"), ("control-node group", "control node's security group")):
            message = message.replace(old, new)
        rows.append("<tr><td>" + esc(label) + "</td><td class='" + esc(check["status"]) + "'>" +
                    esc(statuses.get(check["status"], "Unknown")) + "</td><td>" + esc(message) + "</td></tr>")
    summary = " | ".join(esc(statuses[status]) + ": " + str(report["summary"][status]) for status in statuses)
    cost_section = cost_estimate_section_html(report["cost_estimate"]) if "cost_estimate" in report else ""
    plain_limits = (
        ("Only the resources identified in this manifest", "The listed resources and settings were checked at the time shown."),
        ("Checks do not run continuously", "These checks do not run continuously, correct changes or test scaling. Cost estimates are separate and use the stated assumptions."),
        ("Application database and storage connections", "Application database and storage connections need separate application checks."),
        ("Account identity, instance state", "Other resource settings are outside these checks."),
    )
    limits = "<ul>" + "".join("<li>" + esc(next((plain for prefix, plain in plain_limits if item.startswith(prefix)), item)) + "</li>" for item in report["limitations"]) + "</ul>"
    content = (notice + "<p class='outcome'>" + esc(outcome) + "</p><p>" + summary + "</p>"
               "<h2>Setup details</h2><table class='facts'>" + "".join(metadata) + "</table>"
               "<h2>Created resources</h2><table class='facts'>" + "".join(resources) + "</table>"
               + address_section +
               "<h2>Check results</h2><table class='checks'><thead><tr><th>Check</th><th>Result</th><th>Details</th></tr></thead><tbody>" +
               "".join(rows) + "</tbody></table>" + cost_section + "<h2>Scope of the checks</h2>" + limits)
    return _report_document(title, content)


def write_reports(report, output_dir):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("report.json", json.dumps(_redact(report), ensure_ascii=False, indent=2) + "\n"), ("report.html", report_html(_redact(report)))):
        target = output / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(target)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded_http_get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "AWS-Project-ReadOnly-Check/1.0"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class SnapshotError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__("Offline fixture error")


class SnapshotClient:
    """Offline-only SDK-shaped client; no network operations are implemented."""
    OPERATIONS = {
        "get_caller_identity": "identity",
        "describe_instances": "ec2",
        "describe_security_groups": "security_groups",
        "describe_db_instances": "rds",
        "get_public_access_block": "s3_public_access_block",
        "get_bucket_encryption": "s3_encryption",
        "get_bucket_versioning": "s3_versioning",
    }

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def __getattr__(self, name):
        if name not in self.OPERATIONS:
            raise AttributeError(name)

        def operation(**kwargs):
            self.calls.append((name, kwargs))
            result = self.snapshot.get(self.OPERATIONS[name])
            if not isinstance(result, dict):
                raise SnapshotError("FixtureMissing")
            if "fixture_error" in result:
                raise SnapshotError(result["fixture_error"])
            return result
        return operation


def load_json(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON file is too large")
    return json.loads(data.decode("utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check deployment inputs and produce read-only verification reports.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Offline syntax and selected policy checks; no AWS calls.")
    validate.add_argument("--config", required=True)
    verify = commands.add_parser("verify", help="Inspect specified resources or generate a clearly labelled offline example.")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--output-dir", required=True)
    verify.add_argument("--cost-estimate", help="Attach the exact estimate file bound by cost_estimate_sha256 in the deployment manifest.")
    mode = verify.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Read-only AWS requests and a bounded website HTTP request.")
    mode.add_argument("--fixture", help="Offline sample snapshot. Never constitutes live deployment evidence.")
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            config = load_json(args.config)
        except (OSError, ValueError, UnicodeError):
            print(json.dumps({"valid": False, "errors": [{"field": "config", "message": "Could not read a valid JSON settings file. No file contents are shown."}]}))
            return 2
        errors = validate_config(config)
        print(json.dumps({"valid": not errors, "errors": errors, "scope": "Offline syntax and selected policy checks only. AWS existence, compatibility, permissions, key-file access and quotas require separate preflight checks."}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    try:
        manifest = load_json(args.manifest)
    except (OSError, ValueError, UnicodeError):
        print("Could not read a valid deployment manifest. No file contents are shown.", file=sys.stderr)
        return 2
    errors = validate_manifest(manifest)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    if args.fixture:
        try:
            snapshot = load_json(args.fixture)
            if not isinstance(snapshot, dict):
                raise ValueError()
            client = SnapshotClient(snapshot)
            clients = {name: client for name in ("sts", "ec2", "rds", "s3")}

            def http_get(url):
                result = snapshot.get("http", {})
                if "fixture_error" in result:
                    raise OSError("Offline HTTP failure")
                return result.get("status")
        except (OSError, ValueError, UnicodeError):
            print("Could not read a valid offline fixture. No fixture contents are shown.", file=sys.stderr)
            return 2
    else:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            print("Live checks need boto3. Install requirements-checks.txt in the Linux control-node environment first.", file=sys.stderr)
            return 2
        try:
            session = boto3.Session(region_name=manifest["aws_region"])
            sdk_config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1, "mode": "standard"})
            clients = {name: session.client(name, config=sdk_config) for name in ("sts", "ec2", "rds", "s3")}
            http_get = bounded_http_get
        except Exception:
            print("Could not initialize the AWS read-only clients. Check the control node's credentials and region. No raw service error is shown.", file=sys.stderr)
            return 2
    try:
        report = verify_manifest(manifest, clients, http_get, sample=bool(args.fixture))
    except Exception:
        print("Verification could not process the returned resource data. No raw response or error is shown; no successful report was produced.", file=sys.stderr)
        return 2
    try:
        attach_cost_estimate(report, manifest, args.cost_estimate)
        write_reports(report, args.output_dir)
    except OSError:
        print("Could not save reports in the selected output directory.", file=sys.stderr)
        return 2
    print(json.dumps({"mode": report["mode"], "evidence_notice": report["evidence_notice"], "summary": report["summary"], "checked_scope_passed": report["checked_scope_passed"], "cost_estimate_status": report["cost_estimate"]["status"], "reports": ["report.html", "report.json"]}, ensure_ascii=False, indent=2))
    return 0 if report["checked_scope_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

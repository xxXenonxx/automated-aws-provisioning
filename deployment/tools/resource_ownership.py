#!/usr/bin/env python3
"""Read ownership and location before reusing named database or S3 resources."""
import argparse
import re
import sys


class OwnershipError(ValueError):
    """A safe message without raw AWS responses or account-specific values."""


def _missing(error, codes):
    response = getattr(error, "response", {})
    return isinstance(response, dict) and response.get("Error", {}).get("Code") in codes


def _owned(response, key, config):
    tags = response.get(key) if isinstance(response, dict) else None
    if not isinstance(tags, list):
        raise OwnershipError("The existing resource's ownership tags could not be read.")
    values = {}
    for tag in tags:
        if (not isinstance(tag, dict) or not isinstance(tag.get("Key"), str)
                or not isinstance(tag.get("Value"), str) or tag["Key"] in values):
            raise OwnershipError("The existing resource returned invalid ownership tags.")
        values[tag["Key"]] = tag["Value"]
    if values.get("Project") != config["project_name"] or values.get("Environment") != config["environment_name"]:
        raise OwnershipError("An existing resource is untagged or belongs to another project or environment. Choose unique names or review ownership separately; it was not adopted.")


def check_existing_resources(config, clients, account):
    """Use read-only APIs. Missing resources may be created by the later plays."""
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise OwnershipError("Use the AWS account checked by deployment preflight.")
    try:
        identity = clients["sts"].get_caller_identity()
    except Exception:
        raise OwnershipError("The active AWS account could not be confirmed before ownership checks.") from None
    if not isinstance(identity, dict) or identity.get("Account") != account:
        raise OwnershipError("The AWS account changed before ownership checks. No resources were adopted.")
    region = config["aws_region"]
    rds = clients["rds"]
    try:
        response = rds.describe_db_instances(DBInstanceIdentifier=config["rds_identifier"])
    except Exception as error:
        if not _missing(error, {"DBInstanceNotFound", "DBInstanceNotFoundFault"}):
            raise OwnershipError("Could not establish ownership of the selected database. Check read permissions.") from None
        response = {"DBInstances": []}
    instances = response.get("DBInstances") if isinstance(response, dict) else None
    if not isinstance(instances, list) or len(instances) > 1 or response.get("Marker"):
        raise OwnershipError("The database ownership lookup returned an unsupported result.")
    for instance in instances:
        expected_arn = f"arn:aws:rds:{region}:{account}:db:{config['rds_identifier'].lower()}"
        subnet = instance.get("DBSubnetGroup", {}) if isinstance(instance, dict) else {}
        if (not isinstance(instance, dict) or instance.get("DBInstanceArn") != expected_arn
                or not isinstance(subnet, dict) or subnet.get("VpcId") != config["vpc_id"]
                or str(subnet.get("DBSubnetGroupName", "")).lower() != config["db_subnet_group_name"].lower()):
            raise OwnershipError("The existing database does not match the selected account, region, VPC and subnet group. Review its network change separately.")
        try:
            tags = rds.list_tags_for_resource(ResourceName=expected_arn)
        except Exception:
            raise OwnershipError("The existing database's ownership tags could not be read.") from None
        _owned(tags, "TagList", config)
    try:
        response = rds.describe_db_subnet_groups(DBSubnetGroupName=config["db_subnet_group_name"])
    except Exception as error:
        if not _missing(error, {"DBSubnetGroupNotFound", "DBSubnetGroupNotFoundFault"}):
            raise OwnershipError("Could not establish ownership of the selected database subnet group.") from None
        response = {"DBSubnetGroups": []}
    groups = response.get("DBSubnetGroups") if isinstance(response, dict) else None
    if not isinstance(groups, list) or len(groups) > 1 or response.get("Marker"):
        raise OwnershipError("The database subnet-group ownership lookup returned an unsupported result.")
    for group in groups:
        expected_arn = f"arn:aws:rds:{region}:{account}:subgrp:{config['db_subnet_group_name'].lower()}"
        if not isinstance(group, dict) or group.get("DBSubnetGroupArn") != expected_arn or group.get("VpcId") != config["vpc_id"]:
            raise OwnershipError("The existing database subnet group does not match the selected account, region and VPC.")
        try:
            tags = rds.list_tags_for_resource(ResourceName=expected_arn)
        except Exception:
            raise OwnershipError("The existing database subnet group's ownership tags could not be read.") from None
        _owned(tags, "TagList", config)
    bucket_args = {"Bucket": config["s3_bucket_name"], "ExpectedBucketOwner": account}
    try:
        tags = clients["s3"].get_bucket_tagging(**bucket_args)
    except Exception as error:
        if _missing(error, {"NoSuchBucket", "404", "NotFound"}):
            return {"checked": True, "bucket": "not_created"}
        raise OwnershipError("The bucket is inaccessible or has no ownership tags. Choose a unique bucket name or review its ownership separately.") from None
    _owned(tags, "TagSet", config)
    try:
        location = clients["s3"].get_bucket_location(**bucket_args)
    except Exception:
        raise OwnershipError("The existing bucket's Region could not be checked. No existing bucket was adopted.") from None
    if not isinstance(location, dict) or "LocationConstraint" not in location:
        raise OwnershipError("The bucket Region lookup returned an unsupported result.")
    bucket_region = location["LocationConstraint"] or "us-east-1"
    if bucket_region == "EU":
        bucket_region = "eu-west-1"
    if bucket_region != region:
        raise OwnershipError("The existing bucket uses another AWS Region. Choose a bucket in the priced deployment Region.")
    return {"checked": True, "bucket": "matching_project"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    try:
        from project_checks import load_json, validate_config
        config = load_json(args.config)
        if validate_config(config) or config.get("profile") != "resource_provisioning":
            raise OwnershipError("Use valid resource-provisioning settings before checking ownership.")
        import boto3
        from botocore.config import Config
        session = boto3.Session(region_name=config["aws_region"])
        limits = Config(connect_timeout=5, read_timeout=15, retries={"mode": "standard", "max_attempts": 2})
        clients = {name: session.client(name, config=limits) for name in ("sts", "rds", "s3")}
        check_existing_resources(config, clients, args.account)
    except OwnershipError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Ownership could not be established. Check the settings, dependencies and read permissions; no existing resources were adopted.", file=sys.stderr)
        return 1
    print("Existing resource ownership and location checks passed. No AWS resources were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

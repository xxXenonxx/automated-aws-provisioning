#!/usr/bin/env python3
"""Read ownership and network layout before reusing project resources."""
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
    return values


def _items(response, key):
    if (not isinstance(response, dict) or not isinstance(response.get(key), list)
            or response.get("NextToken") or response.get("Marker")
            or any(not isinstance(item, dict) for item in response[key])):
        raise OwnershipError("A network ownership lookup returned an incomplete or unsupported result.")
    return response[key]


def check_complete_network(config, ec2, account):
    """Only reuse the dedicated, tagged layout created by the complete plays.

    A partial deployment is permitted so a failed creation can be retried. A
    different subnet, route or attachment must be reviewed rather than silently
    adopted or overwritten by that retry. The AWS-created main route table and
    default security group are never managed by these plays.
    """
    prefix = config["project_name"] + "-" + config["environment_name"]
    vpcs = _items(ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [prefix + "-vpc"]}]), "Vpcs")
    if len(vpcs) > 1:
        raise OwnershipError("Several VPCs have this project's network name. Resolve the ambiguity before creation.")
    if not vpcs:
        return {"vpc_id": None, "private_subnet_ids": []}
    vpc = vpcs[0]
    vpc_tags = _owned(vpc, "Tags", config)
    if (not re.fullmatch(r"vpc-(?:[0-9a-f]{8}|[0-9a-f]{17})", str(vpc.get("VpcId", "")))
            or vpc_tags.get("Name") != prefix + "-vpc"
            or vpc.get("OwnerId") != account or vpc.get("IsDefault") is not False
            or vpc.get("CidrBlock") != config["vpc_cidr"]
            or vpc.get("State") != "available"):
        raise OwnershipError("The named VPC has a different owner, CIDR or supported state. It was not adopted.")
    cidrs = vpc.get("CidrBlockAssociationSet", [])
    if (not isinstance(cidrs, list) or len(cidrs) != 1
            or any(item.get("CidrBlock") != config["vpc_cidr"]
            or item.get("CidrBlockState", {}).get("State") != "associated" for item in cidrs)
            or vpc.get("Ipv6CidrBlockAssociationSet")):
        raise OwnershipError("The existing VPC has additional or pending address ranges outside this layout.")
    vpc_id = vpc["VpcId"]
    vpc_filter = [{"Name": "vpc-id", "Values": [vpc_id]}]
    subnets = _items(ec2.describe_subnets(Filters=vpc_filter), "Subnets")
    expected = {
        prefix + "-public-a": config["public_subnet_cidr"],
        prefix + "-private-a": config["private_subnet_a_cidr"],
        prefix + "-private-b": config["private_subnet_b_cidr"],
    }
    by_name = {}
    for subnet in subnets:
        tags = _owned(subnet, "Tags", config)
        name = tags.get("Name")
        if (name not in expected or name in by_name or subnet.get("VpcId") != vpc_id
                or subnet.get("CidrBlock") != expected[name] or subnet.get("State") != "available"
                or not isinstance(subnet.get("AvailabilityZone"), str)
                or subnet.get("Ipv6CidrBlockAssociationSet")
                or subnet.get("MapPublicIpOnLaunch") is not (name == prefix + "-public-a")):
            raise OwnershipError("An existing subnet does not match the approved public/private network layout.")
        by_name[name] = subnet
    public = by_name.get(prefix + "-public-a")
    private_a = by_name.get(prefix + "-private-a")
    private_b = by_name.get(prefix + "-private-b")
    if ((public and private_a and public.get("AvailabilityZone") != private_a.get("AvailabilityZone"))
            or (private_a and private_b and private_a.get("AvailabilityZone") == private_b.get("AvailabilityZone"))
            or (public and private_b and public.get("AvailabilityZone") == private_b.get("AvailabilityZone"))):
        raise OwnershipError("Existing public/private subnets do not preserve the approved two-zone layout.")

    gateways = _items(ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]), "InternetGateways")
    if len(gateways) > 1:
        raise OwnershipError("The project's Internet Gateway attachment is ambiguous.")
    gateway_id = None
    if gateways:
        gateway = gateways[0]
        _owned(gateway, "Tags", config)
        attachments = gateway.get("Attachments", [])
        if len(attachments) != 1 or attachments[0].get("VpcId") != vpc_id or attachments[0].get("State") not in ("available", "attached"):
            raise OwnershipError("The existing Internet Gateway does not match this project's VPC attachment.")
        gateway_id = gateway.get("InternetGatewayId")

    route_tables = _items(ec2.describe_route_tables(Filters=vpc_filter), "RouteTables")
    route_names = set()
    public_ids = {public["SubnetId"]} if public else set()
    private_ids = {subnet["SubnetId"] for subnet in (private_a, private_b) if subnet}
    for table in route_tables:
        if table.get("VpcId") != vpc_id or table.get("PropagatingVgws"):
            raise OwnershipError("A route table has an unexpected VPC or propagated network connection.")
        associations = table.get("Associations", [])
        main = any(item.get("Main") is True for item in associations)
        if main:
            allowed_subnets = set()
            public_routes = False
        else:
            tags = _owned(table, "Tags", config)
            name = tags.get("Name")
            if name not in (prefix + "-public-routes", prefix + "-private-routes") or name in route_names:
                raise OwnershipError("An unexpected or duplicate route table exists in the project VPC.")
            route_names.add(name)
            public_routes = name == prefix + "-public-routes"
            allowed_subnets = public_ids if public_routes else private_ids
        for association in associations:
            if association.get("GatewayId") or (association.get("SubnetId") and association["SubnetId"] not in allowed_subnets):
                raise OwnershipError("A route table has an unexpected subnet or gateway association.")
        for route in table.get("Routes", []):
            local = (route.get("DestinationCidrBlock") == config["vpc_cidr"] and route.get("GatewayId") == "local")
            internet = (public_routes and gateway_id and route.get("DestinationCidrBlock") == "0.0.0.0/0"
                        and route.get("GatewayId") == gateway_id and route.get("State") == "active")
            if not (local or internet):
                raise OwnershipError("The existing network has an unexpected route. Review it before deployment.")
    groups = _items(ec2.describe_security_groups(Filters=vpc_filter), "SecurityGroups")
    seen_groups = set()
    for group in groups:
        if group.get("VpcId") != vpc_id:
            raise OwnershipError("An access group does not belong to the project network.")
        if group.get("GroupName") == "default":
            continue
        _owned(group, "Tags", config)
        name = group.get("GroupName")
        if name not in (prefix + "-web", prefix + "-db") or name in seen_groups:
            raise OwnershipError("An unexpected access group exists in the dedicated project network.")
        seen_groups.add(name)
    return {"vpc_id": vpc_id, "private_subnet_ids": sorted(private_ids)}


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
    complete = config.get("profile") != "resource_provisioning"
    network = check_complete_network(config, clients["ec2"], account) if complete else None
    if complete:
        config = dict(config, vpc_id=network["vpc_id"],
                      db_subnet_group_name=config["project_name"] + "-" + config["environment_name"] + "-db-subnets")
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
        if (not config["vpc_id"] or not isinstance(instance, dict) or instance.get("DBInstanceArn") != expected_arn
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
        if not config["vpc_id"] or not isinstance(group, dict) or group.get("DBSubnetGroupArn") != expected_arn or group.get("VpcId") != config["vpc_id"]:
            raise OwnershipError("The existing database subnet group does not match the selected account, region and VPC.")
        if complete and ({item.get("SubnetIdentifier") for item in group.get("Subnets", [])}
                         != set(network["private_subnet_ids"]) or len(network["private_subnet_ids"]) != 2):
            raise OwnershipError("The existing database subnet group must contain exactly the two project private subnets.")
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
        if validate_config(config):
            raise OwnershipError("Use valid deployment settings before checking ownership.")
        import boto3
        from botocore.config import Config
        session = boto3.Session(region_name=config["aws_region"])
        limits = Config(connect_timeout=5, read_timeout=15, retries={"mode": "standard", "max_attempts": 2})
        services = ("sts", "rds", "s3", "ec2") if config.get("profile") != "resource_provisioning" else ("sts", "rds", "s3")
        clients = {name: session.client(name, config=limits) for name in services}
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

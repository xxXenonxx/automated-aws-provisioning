#!/usr/bin/env python3
"""Read-only MySQL version/support checks before a cost-bound deployment.

No cloud resource is created, modified, started or deleted. This checks engine
selection and standard-support dates; it is not a database-readiness test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys


class PreflightError(ValueError):
    """A safe error without raw AWS response, identifiers or credentials."""


def _utc(value, field):
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PreflightError(f"AWS returned an invalid {field} timestamp; support coverage could not be verified.")
    return value.astimezone(dt.timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _error_code(error):
    response = getattr(error, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        return response["Error"].get("Code")
    return None


def _read(client, operation, **kwargs):
    try:
        result = getattr(client, operation)(**kwargs)
    except Exception:
        raise PreflightError("The read-only RDS lookup failed. Check AWS credentials, Region, network access and permission for the required RDS Describe operations.") from None
    if not isinstance(result, dict):
        raise PreflightError("AWS returned an unsupported RDS response; no supported engine version was selected.")
    if result.get("Marker"):
        raise PreflightError("The exact RDS lookup unexpectedly needs pagination; no supported engine version was selected.")
    return result


def _existing_cost_scope(instance, expected_instance_class, expected_allocated_storage):
    """Reject cost changes or unmodeled performance before a later create play."""
    if expected_instance_class is None:
        return
    if (instance.get("DBInstanceClass") != expected_instance_class
            or instance.get("AllocatedStorage") != expected_allocated_storage
            or instance.get("StorageType") != "gp3"
            or instance.get("MultiAZ") is not False):
        raise PreflightError("The existing database differs from the forecast class, storage size, gp3 type or Single-AZ layout. Review it explicitly rather than reshaping it in this deployment.")
    baseline_iops, baseline_throughput = (3000, 125) if expected_allocated_storage < 400 else (12000, 500)
    # The supported gp3 API returns these properties. Missing information is not
    # proof that custom performance charges are absent.
    if (type(instance.get("Iops")) is not int or instance["Iops"] != baseline_iops
            or type(instance.get("StorageThroughput")) is not int
            or instance["StorageThroughput"] != baseline_throughput):
        raise PreflightError("The existing database has unverified or additional gp3 performance. This forecast covers baseline performance only.")


def resolve_engine_support(client, db_identifier, forecast_days=31, now=None,
                           expected_instance_class=None, expected_allocated_storage=None):
    """Return a safe version decision after bounded, read-only RDS requests."""
    if not isinstance(db_identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,62}", db_identifier) or db_identifier.endswith("-") or "--" in db_identifier:
        raise PreflightError("Use a valid database identifier.")
    if type(forecast_days) is not int or not 1 <= forecast_days <= 3660:
        raise PreflightError("forecast_days must be a whole number from 1 to 3660.")
    if (expected_instance_class is None) != (expected_allocated_storage is None):
        raise PreflightError("Supply both the expected database class and allocated storage, or neither.")
    if expected_instance_class is not None:
        if not isinstance(expected_instance_class, str) or not re.fullmatch(r"db\.[a-z][a-z0-9-]*\.[a-z0-9]+", expected_instance_class):
            raise PreflightError("Use a valid expected RDS instance class.")
        if type(expected_allocated_storage) is not int or not 20 <= expected_allocated_storage <= 65536:
            raise PreflightError("Use a whole expected storage size from 20 to 65536 GiB.")
    if not callable(getattr(client, "describe_db_major_engine_versions", None)):
        raise PreflightError("The installed AWS SDK cannot check RDS support lifecycles. Upgrade boto3 and botocore to at least 1.39.0 before deployment.")
    now = _utc(now or dt.datetime.now(dt.timezone.utc), "current-time")
    source = "existing"
    try:
        response = client.describe_db_instances(DBInstanceIdentifier=db_identifier)
    except Exception as error:
        if _error_code(error) != "DBInstanceNotFound":
            raise PreflightError("The existing-database lookup failed. Check AWS credentials, Region, network access and rds:DescribeDBInstances permission.") from None
        response = {"DBInstances": []}
        source = "regional_default"
    if not isinstance(response, dict) or response.get("Marker"):
        raise PreflightError("AWS returned an unsupported exact database lookup.")
    instances = response.get("DBInstances")
    if not isinstance(instances, list) or len(instances) > 1:
        raise PreflightError("AWS did not return one unambiguous database selection.")
    if instances:
        instance = instances[0]
        if (not isinstance(instance, dict) or not isinstance(instance.get("DBInstanceIdentifier"), str)
                or instance["DBInstanceIdentifier"].lower() != db_identifier.lower()
                or instance.get("Engine") != "mysql"):
            raise PreflightError("The selected existing database is not the requested MySQL database.")
        _existing_cost_scope(instance, expected_instance_class, expected_allocated_storage)
        version = instance.get("EngineVersion")
    else:
        # DescribeDBInstances raises DBInstanceNotFound for a missing exact ID;
        # tolerate an explicitly empty successful response without guessing.
        source = "regional_default"
        defaults = _read(client, "describe_db_engine_versions", Engine="mysql", DefaultOnly=True, MaxRecords=20)
        versions = defaults.get("DBEngineVersions")
        if (not isinstance(versions, list) or len(versions) != 1
                or not isinstance(versions[0], dict) or versions[0].get("Engine") != "mysql"
                or versions[0].get("Status") != "available"):
            raise PreflightError("AWS did not return one available regional default MySQL version.")
        version = versions[0].get("EngineVersion")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9._-]+)?", version):
        raise PreflightError("AWS returned an unsupported MySQL engine version.")
    major = ".".join(version.split(".")[:2])
    lifecycle = _read(client, "describe_db_major_engine_versions", Engine="mysql", MajorEngineVersion=major, MaxRecords=20)
    majors = lifecycle.get("DBMajorEngineVersions")
    if (not isinstance(majors, list) or len(majors) != 1 or not isinstance(majors[0], dict)
            or majors[0].get("Engine") != "mysql" or majors[0].get("MajorEngineVersion") != major):
        raise PreflightError("AWS did not return one matching MySQL support lifecycle.")
    lifecycles = majors[0].get("SupportedEngineLifecycles")
    if not isinstance(lifecycles, list):
        raise PreflightError("AWS did not return standard-support dates for this MySQL major version.")
    standard = [item for item in lifecycles if isinstance(item, dict) and item.get("LifecycleSupportName") == "open-source-rds-standard-support"]
    if len(standard) != 1:
        raise PreflightError("One unambiguous standard-support lifecycle is required before deployment.")
    start = _utc(standard[0].get("LifecycleSupportStartDate"), "standard-support start")
    end = _utc(standard[0].get("LifecycleSupportEndDate"), "standard-support end")
    if start > now or end <= now or end < now + dt.timedelta(days=forecast_days):
        raise PreflightError("The chosen MySQL major version is outside standard support during the forecast period. Extended Support charges are not included; select a supported setup before deployment.")
    return {"engine_version": version, "major_engine_version": major,
            "standard_support_end": _iso(end), "source": source}


def live_client(region):
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client("rds", region_name=region,
                              config=Config(connect_timeout=5, read_timeout=15,
                                            retries={"mode": "standard", "max_attempts": 2}))
    except Exception:
        raise PreflightError("Could not initialize the read-only RDS client. Install the requirements and check the control node's AWS identity.") from None
    return client


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--db-identifier", required=True)
    parser.add_argument("--forecast-days", type=int, default=31)
    parser.add_argument("--instance-class", help="Also require an existing database to match this forecast class.")
    parser.add_argument("--allocated-storage", type=int, help="Expected existing gp3 GiB, with --instance-class.")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", args.region):
            raise PreflightError("Use a valid AWS Region code.")
        result = resolve_engine_support(live_client(args.region), args.db_identifier,
            forecast_days=args.forecast_days, expected_instance_class=args.instance_class,
            expected_allocated_storage=args.allocated_storage)
    except (PreflightError, TypeError, KeyError, OverflowError):
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, PreflightError) else "RDS support coverage could not be verified from the returned data."
        print("RDS preflight failed: " + message, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

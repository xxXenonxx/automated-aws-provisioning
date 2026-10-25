#!/usr/bin/env python3
"""Estimate the supported root deployment from read-only AWS list prices.

This is a forecast, not an account bill or a deployment. Live mode uses only
Pricing.GetProducts; it never creates, changes, or deletes AWS resources. The
offline fixture mode is visibly marked SAMPLE and cannot claim live prices.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys


SCHEMA_VERSION = 1
MAX_JSON_BYTES = 4 * 1024 * 1024
PRICE_ENDPOINT_REGION = "us-east-1"
SOURCES = [
    "https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_pricing_GetProducts.html",
    "https://aws.amazon.com/ec2/pricing/on-demand/",
    "https://aws.amazon.com/ebs/pricing/",
    "https://aws.amazon.com/rds/mysql/pricing/",
    "https://aws.amazon.com/s3/pricing/",
    "https://aws.amazon.com/vpc/pricing/",
]
LIMITATIONS = [
    "Forecast in USD at public On-Demand list prices, not a bill or a guarantee. No Free Tier, credits, Savings Plans, Reserved Instances, negotiated discounts, taxes or support charges are applied.",
    "The selected number of matching Linux EC2 servers, each with one root disk and one public IPv4 address, and one Single-AZ MySQL RDS database are included. Existing VPCs and subnets are reused. There is no load balancer or NAT gateway in this supported workflow.",
    "EC2 root-disk size/type are declared assumptions until compared with the selected AMI or the created EBS volume. Storage is charged for a full retained month even when compute hours are lower.",
    "S3 values are expected average stored GB and monthly request counts, including noncurrent versions. Other storage classes, replication, transfer, retrieval, analytics and account-wide shared tier usage are excluded.",
    "Internet/inter-region/inter-AZ data transfer, burstable CPU credits, extra EBS or RDS IOPS/throughput, snapshots, retained backups and optional monitoring/logging are excluded. Baseline gp3 IOPS/throughput are not charged again.",
    "RDS extended-support charges are excluded: select an engine version in standard support and verify it before deployment. Existing account charges are outside this forecast.",
    "The first month uses the same hours and storage assumptions as the recurring month. These On-Demand items have no separate modeled upfront charge. Actual month length, lifecycle and usage can differ.",
]


class EstimateError(ValueError):
    """A safe, user-facing error without raw SDK exception or credential text."""


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise EstimateError("The JSON input is too large.")
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, parse_float=Decimal)


def canonical_json_sha256(value):
    """Hash sorted, compact UTF-8 JSON using standard JSON number semantics.

    Decimal preserves estimate arithmetic; convert it only for this metadata
    encoding so a normal json.load of the same input produces the same digest.
    The exact estimate-file bytes are separately bound to deployment manifests.
    """
    def encode_number(item):
        if isinstance(item, Decimal):
            return float(item)
        raise TypeError("Unsupported JSON value")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False, default=encode_number)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def input_metadata(config, assumptions):
    result = {"project_name": None, "environment_name": None,
              "config_sha256": None, "assumptions_sha256": None}
    if isinstance(config, dict):
        for key, maximum in (("project_name", 40), ("environment_name", 24)):
            value = config.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0," + str(maximum - 1) + "}", value):
                result[key] = value
        result["config_sha256"] = canonical_json_sha256(config)
    if isinstance(assumptions, dict):
        result["assumptions_sha256"] = canonical_json_sha256(assumptions)
    return result


def decimal_value(value, field, minimum=Decimal("0"), maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise EstimateError(f"{field}: use a finite number.")
    text = str(value)
    if len(text) > 64:
        raise EstimateError(f"{field}: the number is too long.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise EstimateError(f"{field}: use a finite number.") from None
    limit = maximum if maximum is not None else Decimal("1000000000000000000")
    if not number.is_finite() or number < minimum or number > limit:
        raise EstimateError(f"{field}: the value is outside the supported range.")
    return number


def number_text(value):
    return format(value, "f")


def money(value):
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def validate_inputs(config, assumptions):
    if not isinstance(config, dict) or config.get("profile") != "resource_provisioning":
        raise EstimateError("Use the root resource_provisioning environment settings.")
    server_count = config.get("ec2_instance_count", 1)
    if type(server_count) is not int or not 1 <= server_count <= 20:
        raise EstimateError("ec2_instance_count: choose a whole number from 1 to 20; the controller is separate.")
    for field, maximum in (("project_name", 40), ("environment_name", 24)):
        if (not isinstance(config.get(field), str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0," + str(maximum - 1) + "}", config[field])):
            raise EstimateError(f"{field}: use the deployment's name, starting with a letter and containing letters, digits, underscores or hyphens.")
    region = config.get("aws_region")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        raise EstimateError("aws_region: use a valid commercial AWS Region code.")
    if region.startswith(("cn-", "us-gov-", "us-iso")):
        raise EstimateError("This estimator supports commercial AWS Regions only.")
    for field, pattern in (("instance_type", r"[a-z][a-z0-9-]*\.[a-z0-9]+"),
                           ("db_instance_class", r"db\.[a-z][a-z0-9-]*\.[a-z0-9]+")):
        if not isinstance(config.get(field), str) or not re.fullmatch(pattern, config[field]):
            raise EstimateError(f"{field}: use an AWS instance type.")
    storage = decimal_value(config.get("db_allocated_storage"), "db_allocated_storage", Decimal("20"), Decimal("65536"))
    if storage != storage.to_integral_value():
        raise EstimateError("db_allocated_storage: use a whole number of GiB.")
    if not isinstance(assumptions, dict):
        raise EstimateError("Cost assumptions must be a JSON object.")
    required = {"monthly_hours", "ec2_root_volume_gib", "ec2_root_volume_type",
                "s3_standard_storage_gb", "s3_put_requests", "s3_get_requests",
                "include_existing_control_node"}
    if required - assumptions.keys():
        raise EstimateError("Add all required cost assumptions; do not infer unknown disk size or usage.")
    if assumptions.keys() - required - {"control_node"}:
        raise EstimateError("Remove unsupported cost assumptions; do not store secrets here.")
    values = {"monthly_hours": decimal_value(assumptions["monthly_hours"], "monthly_hours", maximum=Decimal("744")),
              "ec2_root_volume_gib": decimal_value(assumptions["ec2_root_volume_gib"], "ec2_root_volume_gib", Decimal("1"), Decimal("65536")),
              "s3_standard_storage_gb": decimal_value(assumptions["s3_standard_storage_gb"], "s3_standard_storage_gb", maximum=Decimal("100000000")),
              "s3_put_requests": decimal_value(assumptions["s3_put_requests"], "s3_put_requests", maximum=Decimal("1000000000000")),
              "s3_get_requests": decimal_value(assumptions["s3_get_requests"], "s3_get_requests", maximum=Decimal("1000000000000"))}
    for field in ("ec2_root_volume_gib", "s3_put_requests", "s3_get_requests"):
        if values[field] != values[field].to_integral_value():
            raise EstimateError(f"{field}: use a whole number.")
    if assumptions["ec2_root_volume_type"] not in ("gp2", "gp3"):
        raise EstimateError("ec2_root_volume_type: this estimator supports gp2 or baseline gp3.")
    if type(assumptions["include_existing_control_node"]) is not bool:
        raise EstimateError("include_existing_control_node: use true or false.")
    values["ec2_root_volume_type"] = assumptions["ec2_root_volume_type"]
    values["include_existing_control_node"] = assumptions["include_existing_control_node"]
    if values["include_existing_control_node"]:
        node = assumptions.get("control_node")
        node_keys = {"instance_type", "root_volume_gib", "root_volume_type", "monthly_hours", "public_ipv4_count"}
        if not isinstance(node, dict) or set(node) != node_keys:
            raise EstimateError("control_node: supply its instance type, disk size/type, hours and public IPv4 count.")
        if not isinstance(node["instance_type"], str) or not re.fullmatch(r"[a-z][a-z0-9-]*\.[a-z0-9]+", node["instance_type"]):
            raise EstimateError("control_node.instance_type: use an AWS instance type.")
        if node["root_volume_type"] not in ("gp2", "gp3"):
            raise EstimateError("control_node.root_volume_type: use gp2 or baseline gp3.")
        node_values = {"instance_type": node["instance_type"], "root_volume_type": node["root_volume_type"],
                       "root_volume_gib": decimal_value(node["root_volume_gib"], "control_node.root_volume_gib", Decimal("1"), Decimal("65536")),
                       "monthly_hours": decimal_value(node["monthly_hours"], "control_node.monthly_hours", maximum=Decimal("744")),
                       "public_ipv4_count": decimal_value(node["public_ipv4_count"], "control_node.public_ipv4_count", maximum=Decimal("10"))}
        for field in ("root_volume_gib", "public_ipv4_count"):
            if node_values[field] != node_values[field].to_integral_value():
                raise EstimateError(f"control_node.{field}: use a whole number.")
        values["control_node"] = node_values
    elif "control_node" in assumptions:
        # An unused optional block is not copied into outputs or treated as cost.
        values["control_node"] = None
    return values


def _filters(attributes, contains=None):
    result = [{"Type": "TERM_MATCH", "Field": key, "Value": value} for key, value in attributes.items()]
    if contains:
        result.append({"Type": "CONTAINS", "Field": "usagetype", "Value": contains})
    return result


class LivePriceList:
    source = "AWS_PRICE_LIST"

    def __init__(self, client=None):
        if client is None:
            try:
                import boto3
                from botocore.config import Config
                client = boto3.client("pricing", region_name=PRICE_ENDPOINT_REGION,
                                      config=Config(connect_timeout=5, read_timeout=20,
                                                    retries={"mode": "standard", "max_attempts": 3}))
            except Exception:
                raise EstimateError("AWS pricing access is unavailable. Install the requirements and use an AWS identity allowed to call pricing:GetProducts.") from None
        self.client = client
        self.cache = {}

    def products(self, service, attributes, contains=None):
        key = (service, tuple(sorted(attributes.items())), contains)
        if key in self.cache:
            return self.cache[key]
        kwargs = {"ServiceCode": service, "Filters": _filters(attributes, contains),
                  "FormatVersion": "aws_v1", "MaxResults": 100}
        products, seen = [], set()
        try:
            for _ in range(50):
                page = self.client.get_products(**kwargs)
                for raw in page.get("PriceList", []):
                    products.append(json.loads(raw) if isinstance(raw, str) else raw)
                token = page.get("NextToken")
                if not token:
                    self.cache[key] = products
                    return products
                if token in seen:
                    raise EstimateError("The AWS price response repeated a pagination token; no estimate was produced.")
                seen.add(token)
                kwargs["NextToken"] = token
        except EstimateError:
            raise
        except Exception:
            raise EstimateError("AWS price lookup failed. Check network access, AWS credentials and pricing:GetProducts permission; no zero-price fallback was used.") from None
        raise EstimateError("AWS returned too many price results; no estimate was produced.")


class SamplePriceList:
    source = "SAMPLE"

    def __init__(self, fixture):
        if (not isinstance(fixture, dict) or fixture.get("sample_only") is not True
                or not isinstance(fixture.get("products"), list)):
            raise EstimateError("Use an explicitly marked SAMPLE pricing fixture.")
        self.data = fixture["products"]

    def products(self, service, attributes, contains=None):
        found = []
        for record in self.data:
            product = record.get("product", {})
            fields = {**product.get("attributes", {}), "productFamily": product.get("productFamily")}
            if record.get("serviceCode") != service:
                continue
            if all(fields.get(key) == value for key, value in attributes.items()):
                if not contains or contains in fields.get("usagetype", ""):
                    found.append(record)
        return found


def _exact_usage(record, suffix):
    usage = record.get("product", {}).get("attributes", {}).get("usagetype", "")
    return usage == suffix or usage.endswith("-" + suffix)


def price_product(provider, service, attributes, suffix=None):
    records = provider.products(service, attributes, suffix)
    # Also verify returned metadata locally; never rely on a mock or API filter
    # alone to price another Region, tenancy, engine, product or usage family.
    matches = []
    for record in records:
        product = record.get("product", {})
        fields = {**product.get("attributes", {}), "productFamily": product.get("productFamily")}
        if record.get("serviceCode") != service or not all(fields.get(key) == value for key, value in attributes.items()):
            continue
        if suffix and not _exact_usage(record, suffix):
            continue
        matches.append(record)
    if len(matches) != 1:
        raise EstimateError(f"Expected one {service} price product but found {len(matches)}. This selected setup cannot be estimated safely; no zero-price fallback was used.")
    return matches[0]


def priced_quantity(record, quantity, expected_unit, now=None):
    """Integrate all price tiers, rejecting gaps/overlaps and future terms."""
    now = now or dt.datetime.now(dt.timezone.utc)
    terms = []
    for term in record.get("terms", {}).get("OnDemand", {}).values():
        try:
            date = dt.datetime.fromisoformat(term["effectiveDate"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            raise EstimateError("AWS returned an invalid price effective date.") from None
        if date.tzinfo is None:
            raise EstimateError("AWS returned a price date without a timezone.")
        if date <= now:
            terms.append((date, term))
    if not terms:
        raise EstimateError("No currently effective On-Demand price is available.")
    latest = max(item[0] for item in terms)
    current = [term for date, term in terms if date == latest]
    if len(current) != 1:
        raise EstimateError("AWS returned ambiguous current price terms.")
    term = current[0]
    tiers = []
    for dimension in term.get("priceDimensions", {}).values():
        if dimension.get("unit") != expected_unit:
            raise EstimateError("AWS price units do not match the quantity being estimated.")
        begin = decimal_value(dimension.get("beginRange"), "price tier start")
        end = dimension.get("endRange")
        end = Decimal("Infinity") if end == "Inf" else decimal_value(end, "price tier end")
        rate = decimal_value(dimension.get("pricePerUnit", {}).get("USD"), "USD rate", maximum=Decimal("1000000000"))
        if end <= begin or dimension.get("appliesTo"):
            raise EstimateError("AWS returned unsupported or invalid price tiers.")
        tiers.append((begin, end, rate, dimension.get("rateCode", "")))
    tiers.sort(key=lambda item: item[0])
    if not tiers or tiers[0][0] != 0:
        raise EstimateError("AWS price tiers do not start at zero.")
    for previous, following in zip(tiers, tiers[1:]):
        if previous[1] != following[0]:
            raise EstimateError("AWS price tiers contain a gap or overlap.")
    if tiers[-1][1] < quantity:
        raise EstimateError("AWS price tiers do not cover the requested quantity.")
    total, breakdown = Decimal("0"), []
    for begin, end, rate, rate_code in tiers:
        used = max(Decimal("0"), min(quantity, end) - begin)
        if used:
            charge = used * rate
            total += charge
            breakdown.append({"quantity": number_text(used), "unit_price_usd": number_text(rate),
                              "amount_usd": number_text(charge), "rate_code": rate_code})
    # Preserve a visible list rate for zero-usage lines, rather than no evidence.
    unit_rate = number_text(tiers[0][2]) if len(tiers) == 1 else None
    return {"amount": total, "unit_price_usd": unit_rate, "tiers": breakdown,
            "effective_date": term["effectiveDate"]}


def estimate(config, assumptions, provider, now=None):
    values = validate_inputs(config, assumptions)
    now = now or dt.datetime.now(dt.timezone.utc)
    region = config["aws_region"]
    lines = []

    def add(key, label, service, attributes, quantity, unit, scope="new_resources", suffix=None):
        product = price_product(provider, service, {"regionCode": region, **attributes}, suffix)
        result = priced_quantity(product, quantity, unit, now)
        lines.append({"key": key, "label": label, "scope": scope, "quantity": number_text(quantity),
                      "unit": unit, "unit_price_usd": result["unit_price_usd"],
                      "monthly_usd": money(result["amount"]), "unrounded_monthly_usd": number_text(result["amount"]),
                      "sku": product["product"]["sku"], "effective_date": result["effective_date"],
                      "publication_date": product.get("publicationDate"), "tiers": result["tiers"]})

    def server(prefix, label, instance_type, volume_type, volume_size, hours, ip_count, scope, count=1):
        add(prefix + "_compute", label + " Linux compute", "AmazonEC2",
            {"productFamily": "Compute Instance", "instanceType": instance_type,
             "operatingSystem": "Linux", "tenancy": "Shared", "preInstalledSw": "NA",
             "capacitystatus": "Used"}, hours * count, "Hrs", scope)
        suffix = "EBS:VolumeUsage.gp3" if volume_type == "gp3" else "EBS:VolumeUsage.gp2"
        add(prefix + "_disk", label + f" {volume_type} root disk (assumed; retained full month)",
            "AmazonEC2", {"productFamily": "Storage"}, volume_size * count, "GB-Mo", scope, suffix)
        if ip_count:
            add(prefix + "_ipv4", label + " public IPv4", "AmazonVPC", {},
                hours * ip_count * count, "Hrs", scope, "PublicIPv4:InUseAddress")

    server("ec2", "Provisioned server", config["instance_type"], values["ec2_root_volume_type"],
           values["ec2_root_volume_gib"], values["monthly_hours"], Decimal("1"), "new_resources",
           count=config.get("ec2_instance_count", 1))
    add("rds_compute", "RDS MySQL Single-AZ compute", "AmazonRDS",
        {"productFamily": "Database Instance", "instanceType": config["db_instance_class"],
         "databaseEngine": "MySQL", "deploymentOption": "Single-AZ"}, values["monthly_hours"], "Hrs",
        suffix="InstanceUsage:" + config["db_instance_class"])
    add("rds_storage", "RDS gp3 allocated storage (baseline performance included)", "AmazonRDS",
        {"productFamily": "Database Storage", "databaseEngine": "MySQL",
         "deploymentOption": "Single-AZ", "volumeType": "General Purpose-GP3"},
        Decimal(str(config["db_allocated_storage"])), "GB-Mo")
    add("s3_storage", "S3 Standard average storage, including retained versions", "AmazonS3",
        {"productFamily": "Storage", "storageClass": "General Purpose", "volumeType": "Standard"},
        values["s3_standard_storage_gb"], "GB-Mo", suffix="TimedStorage-ByteHrs")
    add("s3_put", "S3 Standard PUT / COPY / POST / LIST requests", "AmazonS3",
        {"productFamily": "API Request", "group": "S3-API-Tier1"},
        values["s3_put_requests"], "Requests", suffix="Requests-Tier1")
    add("s3_get", "S3 Standard GET and other Tier 2 requests", "AmazonS3",
        {"productFamily": "API Request", "group": "S3-API-Tier2"},
        values["s3_get_requests"], "Requests", suffix="Requests-Tier2")
    if values["include_existing_control_node"]:
        node = values["control_node"]
        server("controller", "Existing EC2 control node", node["instance_type"], node["root_volume_type"],
               node["root_volume_gib"], node["monthly_hours"], node["public_ipv4_count"], "existing_control_node")
    new_total = sum((Decimal(line["unrounded_monthly_usd"]) for line in lines if line["scope"] == "new_resources"), Decimal("0"))
    controller_total = sum((Decimal(line["unrounded_monthly_usd"]) for line in lines if line["scope"] == "existing_control_node"), Decimal("0"))

    def json_numbers(value):
        if isinstance(value, Decimal):
            return number_text(value)
        if isinstance(value, dict):
            return {key: json_numbers(item) for key, item in value.items()}
        return value

    return {"schema_version": SCHEMA_VERSION, "status": "estimated", "currency": "USD",
            **input_metadata(config, assumptions),
            "source": provider.source, "sample_only": provider.source == "SAMPLE",
            "title": "SAMPLE COST ESTIMATE — demonstration prices only" if provider.source == "SAMPLE" else "AWS list-price cost estimate",
            "generated_at": now.isoformat().replace("+00:00", "Z"), "region": region,
            "price_api_region": PRICE_ENDPOINT_REGION, "assumptions": json_numbers(values),
            "scope": {"profile": "resource_provisioning", "ec2_count": config.get("ec2_instance_count", 1), "rds_engine": "MySQL",
                      "rds_multi_az": False, "rds_storage_type": "gp3", "rds_allocated_storage_gib": int(Decimal(str(config["db_allocated_storage"]))),
                      "new_ec2_instance_type": config["instance_type"], "rds_instance_class": config["db_instance_class"]},
            "line_items": lines, "totals": {"new_resources_monthly_usd": money(new_total),
                "existing_control_node_monthly_usd": money(controller_total),
                "combined_monthly_usd": money(new_total + controller_total),
                "first_month_usd": money(new_total + controller_total), "modeled_upfront_usd": "0.00"},
            "limitations": LIMITATIONS, "sources": SOURCES}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def write_html(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("estimate", help="Forecast supported root resource costs; never deploys anything.")
    command.add_argument("--config", required=True)
    command.add_argument("--assumptions", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--html-output", help="Also save a readable before-deployment forecast; never constitutes live resource verification.")
    modes = command.add_mutually_exclusive_group(required=True)
    modes.add_argument("--live", action="store_true", help="Read current AWS public list prices through Pricing.GetProducts.")
    modes.add_argument("--fixture", help="Use explicitly marked SAMPLE prices; not live evidence.")
    validation = sub.add_parser("validate", help="Check forecast assumptions offline without contacting AWS.")
    validation.add_argument("--config", required=True)
    validation.add_argument("--assumptions", required=True)
    args = parser.parse_args(argv)
    config, assumptions = None, None
    try:
        config = read_json(args.config)
        assumptions = read_json(args.assumptions)
        validate_inputs(config, assumptions)
        if args.command == "validate":
            print("Cost assumptions passed offline checks. Prices, selected AMI disk details and actual AWS usage have not been verified.")
            return 0
        if args.html_output and Path(args.html_output).resolve() == Path(args.output).resolve():
            raise EstimateError("Use different files for the JSON estimate and HTML forecast.")
        provider = LivePriceList() if args.live else SamplePriceList(read_json(args.fixture))
        result = estimate(config, assumptions, provider)
        write_json(args.output, result)
        if args.html_output:
            # Optional presentation layer only; the calculation module has no
            # module-level report import and the report module does not import
            # this estimator. Both direct-script and package use are supported.
            if __package__:
                from .project_checks import cost_estimate_html
            else:
                try:
                    from project_checks import cost_estimate_html
                except ModuleNotFoundError:
                    from tools.project_checks import cost_estimate_html
            write_html(args.html_output, cost_estimate_html(result))
    except (EstimateError, OSError, ValueError, TypeError, KeyError, OverflowError, ImportError):
        # Preserve actionable EstimateError text only. Raw JSON/SDK/file errors
        # can contain user inputs, local account paths or authentication data.
        error = sys.exc_info()[1]
        message = str(error) if isinstance(error, EstimateError) else "Could not read the inputs or write the estimate; check the JSON files and output location."
        if args.command == "validate":
            print("Cost assumptions invalid: " + message, file=sys.stderr)
            return 2
        try:
            metadata = input_metadata(config, assumptions)
        except (ValueError, TypeError, OverflowError):
            metadata = {"project_name": None, "environment_name": None,
                        "config_sha256": None, "assumptions_sha256": None}
        failure = {"schema_version": SCHEMA_VERSION, "status": "error", "currency": "USD",
                   **metadata,
                   "source": "AWS_PRICE_LIST" if args.live else "SAMPLE", "sample_only": not args.live,
                   "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                   "message": message, "totals": None}
        try:
            write_json(args.output, failure)
            if args.html_output and Path(args.html_output).resolve() != Path(args.output).resolve():
                write_html(args.html_output, "<!doctype html><html lang='en'><meta charset='utf-8'><title>Cost estimate unavailable</title><h1>Cost estimate unavailable</h1><p>" + html.escape(message) + "</p><p>No complete estimate or zero-price fallback is available.</p></html>")
        except OSError:
            pass
        print("Cost estimate unavailable: " + message, file=sys.stderr)
        return 2
    label = "SAMPLE — not live prices" if result["sample_only"] else "AWS public list-price forecast"
    print(f"{label}: USD {result['totals']['new_resources_monthly_usd']}/month new resources; "
          f"USD {result['totals']['existing_control_node_monthly_usd']}/month existing control node; "
          f"USD {result['totals']['combined_monthly_usd']}/month combined. Assumptions and exclusions are in the estimate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

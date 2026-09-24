"""Stand the voice agent up on a public HTTPS URL.

What this builds, and why each piece is there:

    EC2 t4g.small (ARM)  runs the app. ~$0.0168/hr, about $8 for twenty days.
    IAM instance role    DynamoDB + Bedrock + S3, from execution-role-policy.json.
    Security group       port 80, from CloudFront's origin-facing ranges only.
    CloudFront           free valid HTTPS on *.cloudfront.net, and WebSockets.

**CloudFront is not optional.** Browsers refuse microphone access outside a secure
context, so an HTTP origin means no voice demo at all. CloudFront supplies a real
certificate on a domain we do not have to buy, which is the whole reason this is
cheap. An ALB would work and costs ~$16/month before any compute.

The instance serves with ``CLINIC_VOICE_ONLY=1``: the doctor's dashboard, calendar,
patient records and documents are not routed at all. Dashboard access is decided by
a ``?role=`` query parameter that is explicitly not a security control, so on a
public URL anyone with the link would otherwise be the doctor, reading patient
names, mobiles and blood groups.

    python deploy/deploy_voice_agent.py --plan      # print what it would do
    python deploy/deploy_voice_agent.py             # build it
    python deploy/deploy_voice_agent.py --teardown  # delete it all

Re-runnable: every resource is looked up by name first and reused.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
NAME = "clinic-voice-agent"
TABLE = "clinic-front-desk"
INSTANCE_TYPE = "t4g.small"
BUNDLE_KEY = "deploy/clinic-front-desk-source.tar.gz"

#: Bucket naming pattern. Resolved from the caller's own account at run time by
#: :func:`resolve_bucket` rather than hardcoded, for two reasons: an account id
#: committed to a public repository cannot be taken back out of git history, and
#: a derived name means this script runs in any account without editing.
#: ``CLINIC_RECORDINGS_BUCKET`` overrides it.
BUCKET_PATTERN = "clinic-recordings-{account}"

#: Filled in :func:`main` once the caller's account is known.
RECORDINGS_BUCKET = ""


def resolve_bucket(account: str) -> str:
    return os.environ.get("CLINIC_RECORDINGS_BUCKET") or BUCKET_PATTERN.format(
        account=account
    )

ROOT = Path(__file__).resolve().parent.parent

#: Files and directories the running container needs. Tests, scripts and local
#: caches are deliberately absent — same reasoning as .dockerignore.
BUNDLE = ("src", "entrypoint.py", "pyproject.toml", "requirements.txt")

TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def policy_document(account: str) -> dict[str, Any]:
    """The execution policy with the placeholders filled in.

    Read from deploy/execution-role-policy.json rather than duplicated here, so the
    permissions the app runs with stay in one reviewable place. The comment keys
    that file carries for humans are stripped — IAM rejects them.
    """
    raw = json.loads((ROOT / "deploy" / "execution-role-policy.json").read_text())
    statements = []
    for statement in raw["Statement"]:
        cleaned = {
            key: value
            for key, value in statement.items()
            if key in {"Sid", "Effect", "Action", "Resource", "Condition"}
        }
        # Drop the KMS statement: recordings use AES256, so there is no key to
        # grant, and a statement naming a non-existent key is noise in an audit.
        if cleaned.get("Sid") == "CallRecordingsKms":
            continue
        statements.append(cleaned)

    document = json.dumps({"Version": raw["Version"], "Statement": statements})
    document = (
        document.replace("ACCOUNT_ID", account)
        .replace("REGION", REGION)
        .replace("TABLE_NAME", TABLE)
        .replace("RECORDINGS_BUCKET", RECORDINGS_BUCKET)
    )
    filled = json.loads(document)
    # The instance also reads its own source bundle out of the same bucket.
    filled["Statement"].append(
        {
            "Sid": "SourceBundle",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": f"arn:aws:s3:::{RECORDINGS_BUCKET}/{BUNDLE_KEY}",
        }
    )
    return filled


def user_data(account: str) -> str:
    """Cloud-init script: install, configure, and keep it running for weeks.

    Written as a systemd unit with Restart=always rather than a bare process,
    because this has to survive twenty days unattended. logrotate is not optional
    either: uvicorn plus the awscrt teardown traceback on every call will fill an
    8 GB disk otherwise. The nightly restart is deliberately blunt — a streaming
    process has never been run here for three weeks straight, and a scheduled
    restart is cheaper than discovering a slow leak during judging.
    """
    return f"""#!/bin/bash
set -euxo pipefail

dnf -y update
dnf -y install python3.12 python3.12-pip tar gzip

install -d -o root -g root /opt/clinic
cd /opt/clinic
aws s3 cp s3://{RECORDINGS_BUCKET}/{BUNDLE_KEY} source.tar.gz --region {REGION}
tar xzf source.tar.gz
rm -f source.tar.gz

python3.12 -m venv /opt/clinic/venv
/opt/clinic/venv/bin/pip install --upgrade pip wheel
/opt/clinic/venv/bin/pip install -r /opt/clinic/requirements.txt
# The [voice] extra is what makes this a voice agent: requirements.txt leaves
# aws-sdk-bedrock-runtime and awscrt commented out so the core package stays
# installable without native builds. Without the extra the page loads, the
# WebSocket upgrades, and then the call dies on ModuleNotFoundError the instant
# Nova Sonic is constructed. [deploy] brings starlette/uvicorn.
/opt/clinic/venv/bin/pip install -e '/opt/clinic[voice,deploy]'
/opt/clinic/venv/bin/python -c 'import aws_sdk_bedrock_runtime, awscrt'

cat >/etc/systemd/system/clinic.service <<'UNIT'
[Unit]
Description=Clinic Front Desk voice agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/clinic
Environment=CLINIC_VOICE_ONLY=1
Environment=CLINIC_TABLE_NAME={TABLE}
Environment=AWS_REGION={REGION}
Environment=CLINIC_RECORDINGS_BUCKET={RECORDINGS_BUCKET}
Environment=CLINIC_DOCUMENTS_BUCKET={RECORDINGS_BUCKET}
Environment=CLINIC_LOG_LEVEL=info
# The live-console shared secret, if one has been set. Deliberately a file rather
# than a literal here: this script is in a public repo, and a secret committed once
# is committed permanently. The leading '-' means a missing file is not an error, so
# the default stays voice-only with no console routed at all.
EnvironmentFile=-/etc/clinic-console.env
ExecStart=/opt/clinic/venv/bin/uvicorn entrypoint:app --host 0.0.0.0 --port 80 \
  --proxy-headers --forwarded-allow-ips='*'
Restart=always
RestartSec=3
StandardOutput=append:/var/log/clinic.log
StandardError=append:/var/log/clinic.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/logrotate.d/clinic <<'ROT'
/var/log/clinic.log {{
    daily
    rotate 3
    maxsize 100M
    compress
    missingok
    notifempty
    copytruncate
}}
ROT

systemctl daemon-reload
systemctl enable --now clinic

# Nightly restart, as a systemd timer rather than cron. AL2023 minimal ships
# without cronie, so /etc/cron.d does not exist -- writing to it under `set -e`
# aborts this whole script, which is exactly how the first deploy installed
# everything and then never started the service. systemd is always present, and
# this now runs after the service is up so it cannot stop the app starting.
cat >/etc/systemd/system/clinic-restart.service <<'RSVC'
[Unit]
Description=Nightly restart of the clinic voice agent

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart clinic
RSVC

cat >/etc/systemd/system/clinic-restart.timer <<'RTIMER'
[Unit]
Description=Restart the clinic voice agent nightly

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
RTIMER

systemctl daemon-reload
systemctl enable --now clinic-restart.timer
"""


def newest_al2023_arm64(ec2: Any) -> str:
    """The newest *standard* Amazon Linux 2023 arm64 image.

    The ``minimal`` variants are excluded on purpose: they ship without
    amazon-ssm-agent, which would leave the instance with no management channel
    at all, given it has no SSH key and accepts traffic only from CloudFront.
    """
    images = [
        image
        for image in ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["al2023-ami-2023*-arm64"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )["Images"]
        if "minimal" not in image["Name"]
    ]
    if not images:
        raise SystemExit("no standard AL2023 arm64 AMI available")
    newest = max(images, key=lambda image: image["CreationDate"])
    print(f"  AMI {newest['ImageId']} ({newest['Name']})")
    return str(newest["ImageId"])


def ensure_role(iam: Any, account: str) -> str:
    try:
        iam.get_role(RoleName=NAME)
        print(f"  role {NAME} exists")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        iam.create_role(
            RoleName=NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST),
            Description="Clinic Front Desk voice agent on EC2",
        )
        print(f"  created role {NAME}")

    iam.put_role_policy(
        RoleName=NAME,
        PolicyName=f"{NAME}-policy",
        PolicyDocument=json.dumps(policy_document(account)),
    )
    print("  attached execution policy")

    # The instance has no SSH key pair and its security group admits only
    # CloudFront, so SSM is the only way to read logs or restart the service
    # during the judging window. Granted at create time, not after: the agent
    # backs off hard if it boots without credentials. See deploy/remote_exec.py.
    iam.attach_role_policy(
        RoleName=NAME,
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )
    print("  attached AmazonSSMManagedInstanceCore (ops channel)")

    try:
        iam.create_instance_profile(InstanceProfileName=NAME)
        print(f"  created instance profile {NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    profile = iam.get_instance_profile(InstanceProfileName=NAME)["InstanceProfile"]
    if not profile["Roles"]:
        iam.add_role_to_instance_profile(InstanceProfileName=NAME, RoleName=NAME)
        print("  bound role to instance profile")
    return NAME


def cloudfront_origin_ranges(ec2: Any) -> list[str]:
    """CloudFront's origin-facing prefix list, so only it can reach port 80.

    Leaving the origin open to the world would let anyone bypass CloudFront and
    hit the instance over plain HTTP — which also defeats the point of having a
    certificate at all.
    """
    lists = ec2.describe_managed_prefix_lists(
        Filters=[
            {
                "Name": "prefix-list-name",
                "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
            }
        ]
    )["PrefixLists"]
    if not lists:
        return []
    return [str(lists[0]["PrefixListId"])]


def ensure_security_group(ec2: Any) -> str:
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpc:
        raise SystemExit("no default VPC in this region")
    vpc_id = vpc[0]["VpcId"]

    existing = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing:
        print(f"  security group {NAME} exists")
        return str(existing[0]["GroupId"])

    group_id = ec2.create_security_group(
        GroupName=NAME,
        Description="Clinic voice agent origin (CloudFront only)",
        VpcId=vpc_id,
    )["GroupId"]

    prefix_lists = cloudfront_origin_ranges(ec2)
    if prefix_lists:
        permission = {
            "IpProtocol": "tcp",
            "FromPort": 80,
            "ToPort": 80,
            "PrefixListIds": [{"PrefixListId": pl} for pl in prefix_lists],
        }
        print("  ingress: port 80 from CloudFront origin-facing ranges only")
    else:
        permission = {
            "IpProtocol": "tcp",
            "FromPort": 80,
            "ToPort": 80,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        }
        print("  ingress: port 80 open (CloudFront prefix list unavailable)")
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[permission])
    print(f"  created security group {group_id}")
    return str(group_id)


def upload_bundle(s3: Any) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in BUNDLE:
            path = ROOT / entry
            if not path.exists():
                raise SystemExit(f"missing from the bundle: {entry}")
            archive.add(path, arcname=entry, filter=_skip_caches)
    payload = buffer.getvalue()
    s3.put_object(Bucket=RECORDINGS_BUCKET, Key=BUNDLE_KEY, Body=payload)
    print(f"  uploaded source bundle ({len(payload) / 1024:.0f} KB)")


def _skip_caches(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if "__pycache__" in info.name or info.name.endswith((".pyc", ".pyo")):
        return None
    return info


def ensure_instance(ec2: Any, profile: str, group_id: str, account: str) -> str:
    running = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )["Reservations"]
    if running:
        instance = running[0]["Instances"][0]
        print(f"  instance {instance['InstanceId']} already running")
        return str(instance["InstanceId"])

    image_id = newest_al2023_arm64(ec2)
    result = ec2.run_instances(
        ImageId=image_id,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": profile},
        SecurityGroupIds=[group_id],
        UserData=user_data(account),
        MetadataOptions={"HttpTokens": "required"},
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}
        ],
    )
    instance_id = str(result["Instances"][0]["InstanceId"])
    print(f"  launched {instance_id} ({INSTANCE_TYPE})")
    return instance_id


def ensure_elastic_ip(ec2: Any, instance_id: str) -> str:
    """A fixed address, so CloudFront's origin survives an instance rebuild.

    A public IPv4 is billed the same whether it is auto-assigned or an Elastic
    IP, so this costs nothing extra. What it buys: if the instance has to be
    replaced mid-judging, the origin hostname does not change, so CloudFront
    needs no update and no re-propagation.
    """
    existing = ec2.describe_addresses(
        Filters=[{"Name": "tag:Name", "Values": [NAME]}]
    )["Addresses"]
    if existing:
        allocation_id = existing[0]["AllocationId"]
        print(f"  reusing Elastic IP {existing[0]['PublicIp']}")
    else:
        allocated = ec2.allocate_address(
            Domain="vpc",
            TagSpecifications=[
                {
                    "ResourceType": "elastic-ip",
                    "Tags": [{"Key": "Name", "Value": NAME}],
                }
            ],
        )
        allocation_id = allocated["AllocationId"]
        print(f"  allocated Elastic IP {allocated['PublicIp']}")

    ec2.associate_address(AllocationId=allocation_id, InstanceId=instance_id)
    address = ec2.describe_addresses(AllocationIds=[allocation_id])["Addresses"][0]
    # CloudFront custom origins need a hostname, not an address.
    dashed = str(address["PublicIp"]).replace(".", "-")
    dns = f"ec2-{dashed}.compute-1.amazonaws.com"
    print(f"  origin {dns}")
    return dns


def ensure_distribution(cloudfront: Any, origin_dns: str) -> tuple[str, str]:
    for item in cloudfront.list_distributions().get("DistributionList", {}).get(
        "Items", []
    ):
        if item.get("Comment") != NAME:
            continue
        distribution_id = str(item["Id"])
        current = cloudfront.get_distribution_config(Id=distribution_id)
        config = current["DistributionConfig"]
        if config["Origins"]["Items"][0]["DomainName"] != origin_dns:
            config["Origins"]["Items"][0]["DomainName"] = origin_dns
            cloudfront.update_distribution(
                Id=distribution_id,
                IfMatch=current["ETag"],
                DistributionConfig=config,
            )
            print(f"  distribution {distribution_id} origin -> {origin_dns}")
        else:
            print(f"  distribution {distribution_id} exists")
        return distribution_id, str(item["DomainName"])

    config = {
        "CallerReference": f"{NAME}-{int(time.time())}",
        "Comment": NAME,
        "Enabled": True,
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": "origin",
                    "DomainName": origin_dns,
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        # The origin has no certificate; CloudFront terminates TLS.
                        "OriginProtocolPolicy": "http-only",
                        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                        "OriginReadTimeout": 60,
                        "OriginKeepaliveTimeout": 60,
                    },
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "origin",
            # Upgrade every request, so the microphone works and the WebSocket
            # can open as wss://.
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 7,
                "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            # CachingDisabled. A cached voice page or a cached WebSocket handshake
            # would be worse than useless.
            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
            # AllViewer: forwards every header, including Upgrade/Connection, which
            # is what makes WebSockets work through CloudFront at all.
            "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3",
            "Compress": True,
        },
        "PriceClass": "PriceClass_100",
    }
    created = cloudfront.create_distribution(DistributionConfig=config)["Distribution"]
    print(f"  created distribution {created['Id']}")
    return str(created["Id"]), str(created["DomainName"])


def teardown() -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    cloudfront = boto3.client("cloudfront")
    iam = boto3.client("iam")

    for item in cloudfront.list_distributions().get("DistributionList", {}).get(
        "Items", []
    ):
        if item.get("Comment") != NAME:
            continue
        distribution_id = item["Id"]
        current = cloudfront.get_distribution_config(Id=distribution_id)
        config = current["DistributionConfig"]
        if config["Enabled"]:
            config["Enabled"] = False
            cloudfront.update_distribution(
                Id=distribution_id, IfMatch=current["ETag"], DistributionConfig=config
            )
            print(f"  disabled distribution {distribution_id}")
            print("    CloudFront takes ~15 minutes to finish; re-run --teardown then")
        else:
            try:
                cloudfront.delete_distribution(
                    Id=distribution_id, IfMatch=current["ETag"]
                )
                print(f"  deleted distribution {distribution_id}")
            except ClientError as exc:
                print(f"  distribution not deletable yet: {exc.response['Error']['Code']}")

    running = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]},
        ]
    )["Reservations"]
    ids = [i["InstanceId"] for r in running for i in r["Instances"]]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        print(f"  terminating {ids}")

    for address in ec2.describe_addresses(
        Filters=[{"Name": "tag:Name", "Values": [NAME]}]
    )["Addresses"]:
        try:
            if address.get("AssociationId"):
                ec2.disassociate_address(AssociationId=address["AssociationId"])
            ec2.release_address(AllocationId=address["AllocationId"])
            print(f"  released Elastic IP {address['PublicIp']}")
        except ClientError as exc:
            print(f"  Elastic IP: {exc.response['Error']['Code']}")

    for name, call in (
        ("role policy", lambda: iam.delete_role_policy(RoleName=NAME, PolicyName=f"{NAME}-policy")),
        ("managed policy", lambda: iam.detach_role_policy(RoleName=NAME, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")),
        ("profile binding", lambda: iam.remove_role_from_instance_profile(InstanceProfileName=NAME, RoleName=NAME)),
        ("instance profile", lambda: iam.delete_instance_profile(InstanceProfileName=NAME)),
        ("role", lambda: iam.delete_role(RoleName=NAME)),
    ):
        try:
            call()
            print(f"  deleted {name}")
        except ClientError as exc:
            print(f"  {name}: {exc.response['Error']['Code']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the plan and stop")
    parser.add_argument("--teardown", action="store_true", help="delete everything")
    args = parser.parse_args()

    account = boto3.client("sts").get_caller_identity()["Account"]
    # Resolved once, here, so no account id needs to live in the source.
    global RECORDINGS_BUCKET
    RECORDINGS_BUCKET = resolve_bucket(account)

    if args.teardown:
        print("TEARDOWN")
        teardown()
        return

    if args.plan:
        print(f"  region          {REGION}")
        print(f"  account         {account}")
        print(f"  instance        {INSTANCE_TYPE} (~$8.06 for 20 days)")
        print(f"  table           {TABLE}")
        print(f"  bucket          {RECORDINGS_BUCKET}")
        print("  public surface  /voice, /ws, /static/*, /ping  (dashboard unrouted)")
        print("  origin ingress  CloudFront origin-facing ranges only")
        print("  https           CloudFront *.cloudfront.net (no domain needed)")
        return

    ec2 = boto3.client("ec2", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    cloudfront = boto3.client("cloudfront")

    print("IAM")
    profile = ensure_role(iam, account)
    print("SOURCE")
    upload_bundle(s3)
    print("NETWORK")
    group_id = ensure_security_group(ec2)
    print("COMPUTE")
    # A new instance profile takes a moment to become usable by RunInstances.
    time.sleep(12)
    instance_id = ensure_instance(ec2, profile, group_id, account)
    print("  waiting for the instance to run ...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    origin = ensure_elastic_ip(ec2, instance_id)
    print("CLOUDFRONT")
    distribution_id, domain = ensure_distribution(cloudfront, origin)

    print()
    print(f"  instance      {instance_id}")
    print(f"  origin        http://{origin}")
    print(f"  distribution  {distribution_id}")
    print()
    print(f"  CALL THE AGENT   https://{domain}/voice")
    print()
    print("  CloudFront takes 5-15 minutes to deploy. The instance needs about")
    print("  4 minutes to install. Check readiness with:")
    print(f"    curl -sS -o /dev/null -w '%{{http_code}}\\n' https://{domain}/ping")


if __name__ == "__main__":
    main()

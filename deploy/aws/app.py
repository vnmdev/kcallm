"""AWS CDK app for the public KcaLLM demo at kcallm.vnmdev.com.

One small ARM EC2 instance in the default VPC runs two containers: Caddy (automatic
HTTPS) and the app in demo mode. There is no SSH port; administration uses SSM. The
image is built locally and pushed to ECR by `cdk deploy`. The model's API key lives
in SSM Parameter Store as a SecureString (/kcallm/demo/api-key) and is created by
hand, so it never appears in this code or the CloudFormation template.
"""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import App, CfnOutput, Environment, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/kcallm/demo"


class KcaLLMDemo(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        domain = self.node.try_get_context("domain") or "kcallm.vnmdev.com"
        model = self.node.try_get_context("model")
        base_url = self.node.try_get_context("base_url") or "https://openrouter.ai/api/v1"
        instance_type = self.node.try_get_context("instance_type") or "t4g.micro"
        if not model:
            raise ValueError("Set the hosted model: cdk deploy -c model=<provider/model-id>")

        image = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(ROOT),
            file="deploy/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )
        for name, value in {
            "image-uri": image.image_uri,
            "model": model,
            "base-url": base_url,
            "domain": domain,
        }.items():
            ssm.StringParameter(
                self, f"Param-{name}", parameter_name=f"{PREFIX}/{name}", string_value=value
            )

        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
        web = ec2.SecurityGroup(
            self, "Web", vpc=vpc, description="KcaLLM demo: HTTP/HTTPS only (no SSH)"
        )
        for port in (80, 443):
            web.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(port))
            web.add_ingress_rule(ec2.Peer.any_ipv6(), ec2.Port.tcp(port))
        web.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.udp(443), "HTTP/3")

        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
            ],
        )
        image.repository.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter{PREFIX}/*"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=["*"],
                conditions={"StringEquals": {"kms:ViaService": f"ssm.{self.region}.amazonaws.com"}},
            )
        )

        user_data = ec2.UserData.for_linux()
        bootstrap = (Path(__file__).parent / "bootstrap.sh").read_text()
        user_data.add_commands(bootstrap.replace("__REGION__", self.region))

        server = ec2.Instance(
            self,
            "Server",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            security_group=web,
            role=role,
            user_data=user_data,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        10, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True
                    ),
                )
            ],
        )
        ip = ec2.CfnEIP(self, "PublicIp", domain="vpc")
        ec2.CfnEIPAssociation(
            self, "PublicIpAssociation", allocation_id=ip.attr_allocation_id,
            instance_id=server.instance_id,
        )

        CfnOutput(self, "ElasticIp", value=ip.attr_public_ip)
        CfnOutput(self, "InstanceId", value=server.instance_id)
        CfnOutput(self, "Url", value=f"https://{domain}")


app = App()
KcaLLMDemo(
    app,
    "KcaLLMDemo",
    env=Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or "eu-north-1",
    ),
)
app.synth()

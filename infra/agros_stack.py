"""Agros voice outreach on AWS.

CloudFront (HTTPS) -> ALB (only accepts requests carrying the CloudFront origin secret)
-> ECS Fargate `api`; Fargate `worker` (dispatch, fixture simulation, timeout sweep) and
optional `agent` (LiveKit voice worker); RDS PostgreSQL 16 in isolated subnets; secrets in
Secrets Manager; Bedrock invoke permissions for the classifier. No NAT gateway: tasks run in
public subnets with public IPs and security groups that allow no inbound except ALB -> api.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parent.parent


class AgrosVoiceStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs: object) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]
        ctx = self.node.try_get_context
        live_voice = str(ctx("liveVoice")).lower() == "true"
        agent_count = int(ctx("agentDesiredCount") or 0)

        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="db", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )

        db_sg = ec2.SecurityGroup(self, "DbSg", vpc=vpc, allow_all_outbound=False)
        tasks_sg = ec2.SecurityGroup(self, "TasksSg", vpc=vpc, allow_all_outbound=True)
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, allow_all_outbound=True)
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "CloudFront origin (header-gated)")
        tasks_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8000), "ALB to api")
        db_sg.add_ingress_rule(tasks_sg, ec2.Port.tcp(5432), "tasks to postgres")

        db = rds.DatabaseInstance(
            self, "Db",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
            vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg], database_name="agros",
            credentials=rds.Credentials.from_generated_secret("agros_owner"),
            allocated_storage=20, storage_encrypted=True, multi_az=False, publicly_accessible=False,
            backup_retention=cdk.Duration.days(7), deletion_protection=False,
            removal_policy=cdk.RemovalPolicy.SNAPSHOT,
        )
        assert db.secret is not None

        def generated(name: str, length: int = 40) -> sm.Secret:
            return sm.Secret(
                self, name,
                generate_secret_string=sm.SecretStringGenerator(
                    password_length=length, exclude_punctuation=True, include_space=False
                ),
            )

        session_secret = generated("SessionSecret")
        webhook_secret = generated("WebhookSecret")
        app_db_password = generated("AppDbPassword", 32)
        seed_password = generated("SeedPassword", 20)
        origin_secret = generated("OriginVerifySecret", 32)
        vendor = sm.Secret(
            self, "VendorSecrets",
            description="LiveKit / Deepgram / Cartesia credentials. Fill in to enable live voice.",
            secret_object_value={
                k: cdk.SecretValue.unsafe_plain_text("")
                for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SIP_TRUNK_ID",
                          "DEEPGRAM_API_KEY", "CARTESIA_API_KEY")
            },
        )

        image = ecs.ContainerImage.from_docker_image_asset(
            ecr_assets.DockerImageAsset(self, "Image", directory=str(REPO_ROOT),
                                        platform=ecr_assets.Platform.LINUX_AMD64)
        )
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights=False)

        alb = elbv2.ApplicationLoadBalancer(self, "Alb", vpc=vpc, internet_facing=True, security_group=alb_sg)
        dist = cf.Distribution(
            self, "Cdn",
            default_behavior=cf.BehaviorOptions(
                origin=origins.LoadBalancerV2Origin(
                    alb, protocol_policy=cf.OriginProtocolPolicy.HTTP_ONLY,
                    custom_headers={"X-Origin-Verify": origin_secret.secret_value.unsafe_unwrap()},
                ),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                cache_policy=cf.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            price_class=cf.PriceClass.PRICE_CLASS_100,
        )
        public_url = f"https://{dist.distribution_domain_name}"

        env = {
            "APP_ENV": "aws",
            "PUBLIC_BASE_URL": public_url,
            "WEBHOOK_BASE_URL": public_url,
            "COOKIE_SECURE": "true",
            "DB_HOST": db.db_instance_endpoint_address,
            "DB_PORT": db.db_instance_endpoint_port,
            "DB_NAME": "agros",
            "VOICE_AGENT_ENABLED": "true",
            "VOICE_AGENT_PROVIDER": "livekit" if live_voice else "fixture",
            "VOICE_LIVE_CALLS_APPROVED": "true" if live_voice else "false",
            "VOICE_LIVE_CALL_ALLOWLIST": str(ctx("liveCallAllowlist") or ""),
            "VOICE_REAL_CONTACTS_APPROVED": "false",
            "VOICE_CALL_RECORDING_ENABLED": "false",
            "VOICE_TRANSCRIPT_RETENTION_ENABLED": "false",
            "CLASSIFIER_MODE": str(ctx("classifierMode") or "rules"),
            "BEDROCK_MODEL_ID": str(ctx("bedrockModelId")),
            "AWS_REGION": self.region,
            "SEED_ADMIN_EMAIL": "admin@agros-demo.test",
        }
        secrets = {
            "DB_MASTER_USER": ecs.Secret.from_secrets_manager(db.secret, "username"),
            "DB_MASTER_PASSWORD": ecs.Secret.from_secrets_manager(db.secret, "password"),
            "APP_DB_PASSWORD": ecs.Secret.from_secrets_manager(app_db_password),
            "SESSION_SECRET": ecs.Secret.from_secrets_manager(session_secret),
            "VOICE_WEBHOOK_SECRET": ecs.Secret.from_secrets_manager(webhook_secret),
            "SEED_PASSWORD": ecs.Secret.from_secrets_manager(seed_password),
        }
        vendor_secrets = {
            k: ecs.Secret.from_secrets_manager(vendor, k)
            for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SIP_TRUNK_ID",
                      "DEEPGRAM_API_KEY", "CARTESIA_API_KEY")
        }

        bedrock = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse"],
            resources=[f"arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:*:{self.account}:inference-profile/*"],
        )

        def service(name: str, command: list[str], cpu: int, mem: int, count: int,
                    extra_secrets: dict[str, ecs.Secret] | None = None, port: int | None = None) -> ecs.FargateService:
            td = ecs.FargateTaskDefinition(self, f"{name}Task", cpu=cpu, memory_limit_mib=mem,
                                           runtime_platform=ecs.RuntimePlatform(
                                               cpu_architecture=ecs.CpuArchitecture.X86_64))
            td.add_to_task_role_policy(bedrock)
            c = td.add_container(
                name, image=image, entry_point=["sh", "-c"], command=[" ".join(command)],
                environment=env, secrets={**secrets, **(extra_secrets or {})},
                logging=ecs.LogDrivers.aws_logs(stream_prefix=name,
                                                log_retention=logs.RetentionDays.ONE_MONTH),
            )
            if port:
                c.add_port_mappings(ecs.PortMapping(container_port=port))
            return ecs.FargateService(
                self, f"{name}Service", cluster=cluster, task_definition=td, desired_count=count,
                assign_public_ip=True, security_groups=[tasks_sg],
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                min_healthy_percent=100 if count > 0 else 0,
            )

        api = service("api", ["agros-voice bootstrap && exec agros-voice api"], 512, 1024, 1,
                      extra_secrets=vendor_secrets if live_voice else None, port=8000)
        worker = service("worker", ["exec agros-voice worker"], 256, 512, 1,
                         extra_secrets=vendor_secrets if live_voice else None)
        agent = service("agent", ["exec agros-voice agent start"], 1024, 2048, agent_count,
                        extra_secrets=vendor_secrets)
        worker.node.add_dependency(api)
        agent.node.add_dependency(api)

        listener = alb.add_listener("Http", port=80, open=False,
                                    default_action=elbv2.ListenerAction.fixed_response(
                                        403, content_type="text/plain", message_body="forbidden"))
        tg = elbv2.ApplicationTargetGroup(
            self, "ApiTg", vpc=vpc, port=8000, protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[api], health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200"),
            deregistration_delay=cdk.Duration.seconds(10),
        )
        listener.add_action(
            "FromCloudFront", priority=1,
            conditions=[elbv2.ListenerCondition.http_header(
                "X-Origin-Verify", [origin_secret.secret_value.unsafe_unwrap()])],
            action=elbv2.ListenerAction.forward([tg]),
        )

        cdk.CfnOutput(self, "AppUrl", value=public_url)
        cdk.CfnOutput(self, "SeedPasswordSecretArn", value=seed_password.secret_arn)
        cdk.CfnOutput(self, "VendorSecretsArn", value=vendor.secret_arn)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "Config", value=json.dumps({"liveVoice": live_voice, "agents": agent_count}))

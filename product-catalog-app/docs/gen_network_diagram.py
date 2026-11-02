from diagrams import Diagram, Cluster, Edge
from diagrams.aws.compute import EKS
from diagrams.aws.database import RDS
from diagrams.aws.network import ALB, InternetGateway, NATGateway, Endpoint, Route53
from diagrams.onprem.network import Internet

graph_attr = {
    "fontsize": "22",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "spline",
}

with Diagram(
    "AWS Network - Product Catalog Platform (PROD, us-east-1)",
    filename="aws_network",
    outformat="pdf",
    show=False,
    graph_attr=graph_attr,
):
    users = Internet("End Users")
    dns = Route53("Route 53\nproduct-catalog.example.com")

    with Cluster("VPC 10.0.0.0/16"):
        igw = InternetGateway("IGW")

        with Cluster("AZ: us-east-1a"):
            with Cluster("Public Subnet\n10.0.0.0/24"):
                alb_a = ALB("ALB target\n(AZ-a)")
                nat_a = NATGateway("NAT GW\n(AZ-a)")
            with Cluster("Private App Subnet\n10.0.10.0/24"):
                eks_a = EKS("EKS Nodes\n(AZ-a)")
            with Cluster("Private Data Subnet\n10.0.20.0/24"):
                rds_a = RDS("RDS Primary")

        with Cluster("AZ: us-east-1b"):
            with Cluster("Public Subnet\n10.0.1.0/24"):
                alb_b = ALB("ALB target\n(AZ-b)")
                nat_b = NATGateway("NAT GW\n(AZ-b)")
            with Cluster("Private App Subnet\n10.0.11.0/24"):
                eks_b = EKS("EKS Nodes\n(AZ-b)")
            with Cluster("Private Data Subnet\n10.0.21.0/24"):
                rds_b = RDS("RDS Standby")

        with Cluster("VPC Endpoints (Interface/Gateway)"):
            ep_ecr = Endpoint("ECR api/dkr")
            ep_s3 = Endpoint("S3 Gateway")
            ep_sm = Endpoint("Secrets Manager")

    users >> Edge(label="HTTPS :443") >> dns
    dns >> Edge(label="resolves to") >> igw
    igw >> Edge(label="inbound") >> [alb_a, alb_b]

    alb_a >> Edge(label="path routing") >> eks_a
    alb_b >> Edge(label="path routing") >> eks_b

    eks_a >> Edge(label="egress", style="dashed") >> nat_a >> Edge(style="dashed") >> igw
    eks_b >> Edge(label="egress", style="dashed") >> nat_b >> Edge(style="dashed") >> igw

    eks_a >> Edge(label=":5432") >> rds_a
    eks_b >> Edge(label=":5432", style="dashed") >> rds_b
    rds_a >> Edge(label="sync replication", style="dashed") >> rds_b

    for node in (eks_a, eks_b):
        for ep in (ep_ecr, ep_s3, ep_sm):
            node >> Edge(label="private route, no NAT", color="darkgreen") >> ep

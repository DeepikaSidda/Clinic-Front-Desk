"""Generate the architecture diagrams for the blog post, with real AWS icons.

Three diagrams rather than one, deliberately. A single picture cannot carry the live
call path, the live handover, the post-call batch path and the notification path
without the arrows crossing each other into soup. Each of these reads left to right
with no crossings:

    architecture_call.png       — how a call works
    architecture_handover.png   — a human takes the call, live
    architecture_aftercall.png  — the record, the transcript, telling the patient

Node labels are kept to two short lines on purpose. ``diagrams`` renders nodes with
``fixedsize=true`` and ``width=1.4``, so a long label overflows its box and collides
with the cluster border or the label next to it. The detail belongs in the edge
labels and in the prose, not stacked under an icon.

Requires:
    pip install diagrams==0.24.4     (Python bindings)
    Graphviz on PATH                 (the `dot` binary)

Run:
    python blog/make_architecture.py
"""

from __future__ import annotations

import pathlib

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2
from diagrams.aws.database import Dynamodb
from diagrams.aws.general import Client, MobileClient, User, Users
from diagrams.aws.integration import SimpleNotificationServiceSns
from diagrams.aws.ml import Bedrock, Polly, Transcribe
from diagrams.aws.network import CloudFront
from diagrams.aws.storage import SimpleStorageServiceS3

HERE = pathlib.Path(__file__).resolve().parent

# One colour per concern, so a reader can follow a single thread through a picture.
AUDIO = "#8C4FFF"  # speech, in either direction
MODEL = "#01A88D"  # a call into a model
DATA = "#232F3E"  # reads and writes of facts
SMS = "#E7157B"  # outbound notification

GRAPH_ATTR = {
    "fontsize": "26",
    "fontname": "Helvetica bold",
    "bgcolor": "white",
    "pad": "0.5",
    # Generous, because a node's label overflows its fixed box: this is the room
    # that stops one label landing on top of the next.
    "nodesep": "1.0",
    "ranksep": "2.0",
    "splines": "spline",
}
NODE_ATTR = {"fontsize": "14", "fontname": "Helvetica"}
EDGE_ATTR = {"fontsize": "12", "fontname": "Helvetica"}
# Margin keeps a node's overflowing label off the cluster's own border.
CLUSTER_ATTR = {"fontsize": "15", "fontname": "Helvetica bold", "margin": "36"}


def _diagram(title: str, filename: str) -> Diagram:
    return Diagram(
        title,
        filename=str(HERE / filename),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
        node_attr=NODE_ATTR,
        edge_attr=EDGE_ATTR,
    )


def call_diagram() -> None:
    """How a call works. Six nodes, one direction, no crossings."""
    with _diagram(
        "How a call works  ·  the agent decides what to say, never what is true",
        "architecture_call",
    ):
        patient = Users("Patient\nbrowser + mic")

        with Cluster("AWS Cloud  ·  us-east-1", graph_attr=CLUSTER_ATTR):
            cdn = CloudFront("CloudFront\nTLS")

            with Cluster("EC2 t4g.small", graph_attr=CLUSTER_ATTR):
                voice = EC2("Voice_Front_Desk\n12 tools")

            # Declared bottom-up: dot stacks the last cluster highest, and the
            # speech-to-speech stream is the one that should sit beside the agent.
            with Cluster("Data_Layer", graph_attr=CLUSTER_ATTR):
                bucket = SimpleStorageServiceS3("S3\nstereo WAV")
                table = Dynamodb("DynamoDB\none table")

            with Cluster("Amazon Bedrock", graph_attr=CLUSTER_ATTR):
                titan = Bedrock("Titan Embeddings\nclinic PDFs")
                sonic = Bedrock("Nova Sonic\nspeech-to-speech")

        patient >> Edge(color=AUDIO, penwidth="3", label="16 kHz up\n24 kHz down") >> cdn
        cdn >> Edge(color=AUDIO, penwidth="3", label="wss://") >> voice
        (
            voice
            >> Edge(
                color=AUDIO,
                penwidth="3",
                label="bidirectional\naudio stream",
                forward=True,
                reverse=True,
            )
            >> sonic
        )
        voice >> Edge(color=MODEL, label="clinic questions") >> titan
        (
            voice
            >> Edge(
                color=DATA,
                penwidth="2",
                label="12 tools —\nevery spoken fact",
                forward=True,
                reverse=True,
            )
            >> table
        )
        voice >> Edge(color=DATA, label="call recording") >> bucket


def handover_diagram() -> None:
    """A human takes the call, live, while the caller is still on the line."""
    with _diagram("A human takes the call, live", "architecture_handover"):
        doctor = User("Doctor\nbrowser + mic")

        with Cluster("AWS Cloud  ·  us-east-1", graph_attr=CLUSTER_ATTR):
            with Cluster("EC2 t4g.small", graph_attr=CLUSTER_ATTR):
                live = Client("Live console\n/live?role=doctor")
                voice = EC2("Voice_Front_Desk\nfed silence")

            polly = Polly("Amazon Polly\ntyped → spoken")

        patient = Users("Patient\nstill on the line")

        doctor >> Edge(color=AUDIO, penwidth="3", label="her own voice") >> live
        (
            live
            >> Edge(
                color=AUDIO,
                penwidth="3",
                label="agent stops listening,\nnot just speaking",
                forward=True,
                reverse=True,
            )
            >> voice
        )
        live >> Edge(color=MODEL, label="she types instead") >> polly
        polly >> Edge(color=AUDIO, penwidth="2", label="same audio channel") >> voice
        (
            voice
            >> Edge(color=AUDIO, penwidth="3", label="two-way", forward=True, reverse=True)
            >> patient
        )


def aftercall_diagram() -> None:
    """The durable record, the transcript, and telling the patient."""
    with _diagram(
        "After the call  ·  the record, the transcript, telling the patient",
        "architecture_aftercall",
    ):
        with Cluster("AWS Cloud  ·  us-east-1", graph_attr=CLUSTER_ATTR):
            with Cluster("EC2 t4g.small", graph_attr=CLUSTER_ATTR):
                voice = EC2("Voice_Front_Desk")
                dash = Client("Doctor dashboard\ncancel & notify")

            bucket = SimpleStorageServiceS3("S3\nstereo WAV")
            scribe = Transcribe("Transcribe\nper channel")
            table = Dynamodb("DynamoDB\ncall record")
            sns = SimpleNotificationServiceSns("SNS\ncancellation SMS")

        phone = MobileClient("Patient's mobile")

        voice >> Edge(color=DATA, penwidth="2", label="one WAV per call,\nboth voices") >> bucket
        bucket >> Edge(color=MODEL, label="reads the recording") >> scribe
        scribe >> Edge(color=MODEL, label="labelled transcript") >> table
        dash >> Edge(color=DATA, penwidth="2", label="cancellation\nwritten first") >> table
        dash >> Edge(color=SMS, penwidth="2", label="then notify") >> sns
        sns >> Edge(color=SMS, penwidth="2", label="SMS") >> phone


if __name__ == "__main__":
    call_diagram()
    handover_diagram()
    aftercall_diagram()
    for name in (
        "architecture_call.png",
        "architecture_handover.png",
        "architecture_aftercall.png",
    ):
        print(f"wrote {HERE / name}")

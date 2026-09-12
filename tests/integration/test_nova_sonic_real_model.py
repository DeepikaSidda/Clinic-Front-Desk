"""Integration check: the voice adapter builds a *real* Nova Sonic model.

This proves the Voice_Front_Desk genuinely integrates Amazon Nova Sonic
speech-to-speech over Amazon Bedrock (via the Strands ``BidiNovaSonicModel``),
not just a fake. It is skipped automatically when the optional ``voice`` extra
(``aws-sdk-bedrock-runtime`` + ``awscrt``) is not installed, so the core suite
still runs without AWS-native dependencies.

Model *construction* needs no network or credentials (no Bedrock connection is
opened until ``start()``), so this runs offline in CI once the extra is present.

Install the extra with:  pip install -e ".[voice]"
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "aws_sdk_bedrock_runtime",
    reason="voice extra not installed; run `pip install -e .[voice]` for the real Nova Sonic path",
)
pytest.importorskip("awscrt", reason="awscrt (AWS Common Runtime) not installed")

from clinic_front_desk.voice.stream import NovaSonicVoiceStream  # noqa: E402

pytestmark = pytest.mark.integration


def test_adapter_builds_real_bidi_nova_sonic_model() -> None:
    """The adapter's default model is a genuine BidiNovaSonicModel on Bedrock."""
    from strands.experimental.bidi.models.nova_sonic import (
        NOVA_SONIC_V2_MODEL_ID,
        BidiNovaSonicModel,
    )

    stream = NovaSonicVoiceStream(region="us-east-1", voice_id="matthew")
    model = stream._build_nova_sonic_model()

    assert isinstance(model, BidiNovaSonicModel)
    # Defaults to the Nova Sonic v2 speech-to-speech model id.
    assert model.model_id == NOVA_SONIC_V2_MODEL_ID
    assert model.region == "us-east-1"
    # Voice + v2 turn detection flow through into the provider config.
    assert model.config["audio"]["voice"] == "matthew"
    assert model.config["turn_detection"]["endpointingSensitivity"] == "MEDIUM"


def test_adapter_honours_custom_model_id_and_voice() -> None:
    from strands.experimental.bidi.models.nova_sonic import (
        NOVA_SONIC_V1_MODEL_ID,
        BidiNovaSonicModel,
    )

    # v1 does not support turn_detection, so exercise it with sensitivity off.
    stream = NovaSonicVoiceStream(
        model_id=NOVA_SONIC_V1_MODEL_ID,
        region="us-west-2",
        voice_id="tiffany",
        provider_config={"turn_detection": {}},
    )
    model = stream._build_nova_sonic_model()

    assert isinstance(model, BidiNovaSonicModel)
    assert model.model_id == NOVA_SONIC_V1_MODEL_ID
    assert model.region == "us-west-2"
    assert model.config["audio"]["voice"] == "tiffany"

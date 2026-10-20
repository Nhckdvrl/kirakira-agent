"""Step Token Plan as an ordinary native Models driver contribution."""

from dataclasses import replace
from urllib.parse import urlsplit

from plugins.models.contract import (
    CapabilitySources,
    DiscoveredModel,
    DriverConnection,
    ModelCapabilities,
    ModelDriverDefinition,
    ModelRequest,
)

from .transport import _open

DRIVER_ID = "step-plan"
BASE_URL = "https://api.stepfun.com/step_plan/v1"
MODEL = "step-5-preview"


def validate_endpoint(endpoint):
    value = urlsplit(endpoint)
    if (value.scheme != "https" or value.netloc not in {"api.stepfun.com", "api.stepfun.ai"}
            or value.path.rstrip("/") != "/step_plan/v1" or value.query or value.fragment):
        raise ValueError("Step Token Plan requires its dedicated HTTPS /step_plan/v1 endpoint")
    return endpoint.rstrip("/")


async def open_connection(descriptor, credential):
    if descriptor.driver_id != DRIVER_ID:
        raise ValueError("connection does not belong to the Step Token Plan driver")
    endpoint = validate_endpoint(descriptor.endpoint)
    if descriptor.config.get("thinking_format", "none") != "none":
        raise ValueError("Step5 uses reasoning_effort")
    # The imported transport never follows redirects. Models remains the sole
    # owner of attempts, call receipts, streaming reset and bound credentials.
    inner = await _open(replace(descriptor, endpoint=endpoint, driver_id="openai-compatible"), credential)

    def chat(model, config):
        if model.driver_id != DRIVER_ID or model.model not in {"step5", MODEL}:
            raise ValueError("Step Token Plan is bound to step-5-preview")
        effort = model.reasoning_effort or "medium"
        if effort not in {"low", "medium", "high"}:
            raise ValueError("Step5 reasoning_effort must be low, medium or high")
        return inner.bind_chat(replace(model, driver_id="openai-compatible", model=MODEL,
                                       reasoning_effort=effort), config)

    def embedding(*args, **kwargs):
        raise ValueError("this Step Token Plan driver grants chat only")

    return DriverConnection(chat, embedding, inner.aclose)


async def discover(descriptor, credential):
    validate_endpoint(descriptor.endpoint)
    return (DiscoveredModel(
        kind="chat", model=MODEL, default_reasoning_effort="medium",
        capabilities=ModelCapabilities(
            supports_tool_calls=True, supports_parallel_tool_calls=True,
            supported_reasoning_efforts=("low", "medium", "high")),
        capability_sources=CapabilitySources(tool_calls="driver",
                                              parallel_tool_calls="driver",
                                              reasoning_efforts="driver"),
    ),)


async def probe(descriptor, credential):
    from plugins.models.contract import BoundModelDescriptor
    connection = await open_connection(descriptor, credential)
    try:
        bound = BoundModelDescriptor(
            binding_id="step-plan-probe", plugin_snapshot_id="probe", model_revision=0,
            model_id="step5", connection_id=descriptor.connection_id, driver_id=DRIVER_ID,
            driver_contract_version="1", auth_identity=descriptor.auth_identity,
            model=MODEL, role="default", reasoning_effort="low",
            capabilities=ModelCapabilities(), capability_sources=CapabilitySources(),
            capability_digest="probe",
        )
        await connection.bind_chat(bound, {}).complete(ModelRequest(
            messages=({"role": "user", "content": "Reply OK."},), max_output_tokens=128))
    finally:
        await connection.aclose()


def definition():
    return ModelDriverDefinition(DRIVER_ID, "1", open_connection, discover=discover, probe=probe)

"""Step Plan transport with an explicit subscription-only routing boundary."""

import urllib.error
import urllib.request
from urllib.parse import urlsplit

from agent.model_runtime.retry import retry_after_seconds
from agent.model_runtime.types import RetryableModelError
from infra.providers.llm_provider import OpenAICompatibleClient

STEP_PLAN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
STEP5_MODEL = "step-5-preview"


def validate_step_plan_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"api.stepfun.com", "api.stepfun.ai"}
        or parsed.path.rstrip("/") != "/step_plan/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Step Plan requires its dedicated HTTPS /step_plan/v1 endpoint"
        )
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError(
            "Step Plan does not follow redirects to another billing surface"
        )


class StepPlanClient(OpenAICompatibleClient):
    billing_surface = "step_plan"

    def __init__(
        self, *, base_url=STEP_PLAN_BASE_URL, reasoning_effort="medium", **kwargs
    ):
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("Step5 reasoning_effort must be low, medium, or high")
        self.reasoning_effort = reasoning_effort
        super().__init__(base_url=validate_step_plan_url(base_url), **kwargs)

    def _chat_completions_url(self):
        return validate_step_plan_url(self.base_url) + "/chat/completions"

    def _build_payload(self, messages, tools, system, model, max_tokens, **kwargs):
        payload = super()._build_payload(
            messages,
            tools,
            system,
            STEP5_MODEL if model == "step5" else model,
            max_tokens,
            **kwargs,
        )

        if payload["model"] == STEP5_MODEL:
            # Step5 documents reasoning_effort, not DeepSeek's thinking object.
            payload.pop("thinking", None)
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _open(self, payload):
        import json

        request = urllib.request.Request(
            self._chat_completions_url(),
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            return urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=self.timeout
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise self._http_error_for(exc.code, detail) or RetryableModelError(
                f"Step Plan HTTP {exc.code}: {detail}",
                status_code=exc.code,
                retry_after=retry_after_seconds(exc.headers.get("Retry-After")),
            ) from exc
        except urllib.error.URLError as exc:
            raise RetryableModelError(
                f"Step Plan transport failed: {exc.reason}"
            ) from exc


def create_model_client(*, reasoning_effort="medium", **kwargs):
    url = str(kwargs.get("base_url") or "")
    client_type = (
        StepPlanClient if "/step_plan" in urlsplit(url).path else OpenAICompatibleClient
    )
    if client_type is StepPlanClient:
        kwargs["reasoning_effort"] = reasoning_effort
    return client_type(**kwargs)

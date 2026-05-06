import json
import unittest

from kirakira_agent.models.openai_compatible import OpenAICompatibleClient
from kirakira_agent.schema import ModelResponse, ToolCall, ToolResult, ToolSpec, assistant_message_from_response, tool_result_message


class OpenAICompatibleTests(unittest.TestCase):
    def test_parse_tool_call_response(self):
        client = OpenAICompatibleClient(base_url="http://example.test/v1", api_key="")
        payload = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "README.md"}),
                                },
                            }
                        ],
                    },
                }
            ]
        }
        response = client.parse_response(payload)

        self.assertEqual(response.stop_reason, "tool_use")
        self.assertEqual(response.tool_calls[0].name, "read_file")
        self.assertEqual(response.tool_calls[0].arguments["path"], "README.md")

    def test_tool_result_message_shape(self):
        message = tool_result_message(ToolResult("call_1", "done", False))

        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["tool_call_id"], "call_1")
        self.assertEqual(message["content"], "done")

    def test_to_openai_messages_serializes_tool_call_arguments(self):
        client = OpenAICompatibleClient(base_url="http://example.test/v1", api_key="")
        response = ModelResponse(tool_calls=[ToolCall("call_1", "bash", {"command": "pwd"})])
        messages = [assistant_message_from_response(response)]

        converted = client._to_openai_messages(messages, system="sys")

        self.assertEqual(converted[0]["role"], "system")
        args = converted[1]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(args), {"command": "pwd"})


if __name__ == "__main__":
    unittest.main()

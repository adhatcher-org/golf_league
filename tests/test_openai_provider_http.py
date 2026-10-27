"""W2: OpenAIVisionProvider tests with synthetic HTTP responses."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from golf_league.services.scorecard_vision import (
    OpenAIVisionProvider,
    ProviderOutputError,
    ProviderUnavailable,
)


class MockOpenAIHandler(BaseHTTPRequestHandler):
    """Mock OpenAI API handler for testing."""

    def __init__(self, *args, **kwargs):
        self.test_data = kwargs.pop('test_data', {})
        super().__init__(*args, **kwargs)

    def _has_image_url(self, payload: dict) -> bool:
        for msg in payload.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "image_url":
                        return True
        return False

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        """Handle POST requests to /chat/completions."""
        if self.path != "/chat/completions":
            self._respond(404)
            return

        auth_header = self.headers.get("Authorization")
        if auth_header != "Bearer test-key-123":
            self._respond(401)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body)
        except Exception:
            self._respond(500)
            return

        if "model" not in payload or "messages" not in payload:
            self._respond(400)
            return

        if not self._has_image_url(payload):
            self._respond(400)
            return

        test_case = self.test_data.get("case", "success")

        if test_case == "success":
            response = {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "players": [
                                {
                                    "name": "John Doe",
                                    "scores": [3, 4, 5, 4, 5],
                                    "uncertain": [False, False, False, False, False],
                                }
                            ],
                            "holes_header": ["1", "2", "3", "4", "5"],
                        })
                    }
                }]
            }
            self._respond(200, json.dumps(response).encode("utf-8"))
        elif test_case == "bad_json":
            self._respond(200, b"{invalid json}")
        elif test_case == "wrong_structure":
            response = {
                "choices": [{
                    "message": {
                        "content": "plain text"
                    }
                }]
            }
            self._respond(200, json.dumps(response).encode("utf-8"))
        elif test_case == "timeout":
            time.sleep(10)
            self._respond(200)

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass


def test_openai_provider_success():
    """Test successful OpenAI provider request."""
    # Start mock server
    server = HTTPServer(('localhost', 0), lambda *args, **kwargs: MockOpenAIHandler(*args, test_data={"case": "success"}, **kwargs))
    port = server.server_port

    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    try:
        provider = OpenAIVisionProvider(
            api_key="test-key-123",
            model="gpt-4-vision",
            base_url=f"http://localhost:{port}",
            connect_timeout=5.0,
            total_timeout=5.0,
        )

        # Create a small test image (1x1 pixel JPEG)
        test_image = b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\t\x07\x06\x0C\r\x0B\x0B\x09\x0E\x0E\r\x0F\x14\r\x10\x0A\x10\x11\x11\x0A\x0E\x1D\r\x1D\x1E\x1F\x1E\x1D\x1C\x1C\x1B\x1B\x1C\x18\x19\x1A\x1F\x1F\x1E\x1E\x1D\x1C\x1B\x1A\x19\x19\x18\x17\x16\x15\x14\x13\x12\x11\x10\x0F\x0E\x0D\x0C\x0B\x0A\x09\x08\x07\x06\x05\x04\x03\x02\x01\xFF\xC0\x00\x0B\x08\x00\x01\x00\x01\x01\x01\x11\x00\xFF\xC4\x00\x1F\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0B\xFF\xC4\x00\xB5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x06\x06\x07\x07\x08\t\n\x0B\x0C\r\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00\xFF\xD9'

        # Test read_scorecard
        card = provider.read_scorecard(test_image, hole_count=5)

        assert card.players[0].name == "John Doe"
        assert card.players[0].scores == (3, 4, 5, 4, 5)
        assert card.model_name == "gpt-4-vision"

    finally:
        server.shutdown()


def test_openai_provider_bad_url():
    """Test provider with invalid URL."""
    provider = OpenAIVisionProvider(
        api_key="test-key-123",
        model="gpt-4-vision",
        base_url="invalid-url",
    )

    test_image = b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\t\x07\x06\x0C\r\x0B\x0B\x09\x0E\x0E\r\x0F\x14\r\x10\x0A\x10\x11\x11\x0A\x0E\x1D\r\x1D\x1E\x1F\x1E\x1D\x1C\x1C\x1B\x1B\x1C\x18\x19\x1A\x1F\x1F\x1E\x1E\x1D\x1C\x1B\x1A\x19\x19\x18\x17\x16\x15\x14\x13\x12\x11\x10\x0F\x0E\x0D\x0C\x0B\x0A\x09\x08\x07\x06\x05\x04\x03\x02\x01\xFF\xC0\x00\x0B\x08\x00\x01\x00\x01\x01\x01\x11\x00\xFF\xC4\x00\x1F\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0B\xFF\xC4\x00\xB5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x06\x06\x07\x07\x08\t\n\x0B\x0C\r\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00\xFF\xD9'

    try:
        provider.read_scorecard(test_image, hole_count=5)
    except ProviderUnavailable as e:
        assert e.code == "provider_bad_url"
    else:
        raise AssertionError("Should have raised ProviderUnavailable")


def test_openai_provider_wrong_auth():
    """Test provider with wrong authorization."""
    server = HTTPServer(('localhost', 0), lambda *args, **kwargs: MockOpenAIHandler(*args, test_data={"case": "success"}, **kwargs))
    port = server.server_port

    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    try:
        provider = OpenAIVisionProvider(
            api_key="wrong-key",
            model="gpt-4-vision",
            base_url=f"http://localhost:{port}",
            connect_timeout=5.0,
            total_timeout=5.0,
        )

        test_image = b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\t\x07\x06\x0C\r\x0B\x0B\x09\x0E\x0E\r\x0F\x14\r\x10\x0A\x10\x11\x11\x0A\x0E\x1D\r\x1D\x1E\x1F\x1E\x1D\x1C\x1C\x1B\x1B\x1C\x18\x19\x1A\x1F\x1F\x1E\x1E\x1D\x1C\x1B\x1A\x19\x19\x18\x17\x16\x15\x14\x13\x12\x11\x10\x0F\x0E\x0D\x0C\x0B\x0A\x09\x08\x07\x06\x05\x04\x03\x02\x01\xFF\xC0\x00\x0B\x08\x00\x01\x00\x01\x01\x01\x11\x00\xFF\xC4\x00\x1F\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0B\xFF\xC4\x00\xB5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x06\x06\x07\x07\x08\t\n\x0B\x0C\r\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00\xFF\xD9'

        try:
            provider.read_scorecard(test_image, hole_count=5)
        except ProviderUnavailable:
            pass  # Expected
        else:
            raise AssertionError("Should have raised ProviderUnavailable")

    finally:
        server.shutdown()


def test_openai_provider_bad_json_response():
    """Test provider with invalid JSON response."""
    server = HTTPServer(('localhost', 0), lambda *args, **kwargs: MockOpenAIHandler(*args, test_data={"case": "bad_json"}, **kwargs))
    port = server.server_port

    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    try:
        provider = OpenAIVisionProvider(
            api_key="test-key-123",
            model="gpt-4-vision",
            base_url=f"http://localhost:{port}",
            connect_timeout=5.0,
            total_timeout=5.0,
        )

        test_image = b'\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\t\x07\x06\x0C\r\x0B\x0B\x09\x0E\x0E\r\x0F\x14\r\x10\x0A\x10\x11\x11\x0A\x0E\x1D\r\x1D\x1E\x1F\x1E\x1D\x1C\x1C\x1B\x1B\x1C\x18\x19\x1A\x1F\x1F\x1E\x1E\x1D\x1C\x1B\x1A\x19\x19\x18\x17\x16\x15\x14\x13\x12\x11\x10\x0F\x0E\x0D\x0C\x0B\x0A\x09\x08\x07\x06\x05\x04\x03\x02\x01\xFF\xC0\x00\x0B\x08\x00\x01\x00\x01\x01\x01\x11\x00\xFF\xC4\x00\x1F\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0B\xFF\xC4\x00\xB5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x06\x06\x07\x07\x08\t\n\x0B\x0C\r\x0E\x0F\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1A\xFF\xDA\x00\x08\x01\x01\x00\x00?\x00\xFF\xD9'

        try:
            provider.read_scorecard(test_image, hole_count=5)
        except ProviderOutputError as e:
            assert e.code == "output_envelope"
        else:
            raise AssertionError("Should have raised ProviderOutputError")

    finally:
        server.shutdown()


if __name__ == "__main__":
    test_openai_provider_success()
    test_openai_provider_bad_url()
    test_openai_provider_wrong_auth()
    test_openai_provider_bad_json_response()
    print("All W2 provider tests passed!")

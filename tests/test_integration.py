import sys
from threading import Thread, Lock
import json
import warnings

import stripe
import pytest

if sys.version_info[0] < 3:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
else:
    from http.server import BaseHTTPRequestHandler, HTTPServer


class TestIntegration(object):
    @pytest.fixture(autouse=True)
    def close_mock_server(self):
        yield
        if self.mock_server:
            self.mock_server.shutdown()
            self.mock_server.server_close()
            self.mock_server_thread.join()

    @pytest.fixture(autouse=True)
    def setup_stripe(self):
        orig_attrs = {
            "api_base": stripe.api_base,
            "api_key": stripe.api_key,
            "default_http_client": stripe.default_http_client,
            "max_network_retries": stripe.max_network_retries,
            "proxy": stripe.proxy,
        }
        stripe.api_base = "http://localhost:12111"  # stripe-mock
        stripe.api_key = "sk_test_123"
        stripe.default_http_client = None
        stripe.max_network_retries = 3
        stripe.proxy = None
        yield
        stripe.api_base = orig_attrs["api_base"]
        stripe.api_key = orig_attrs["api_key"]
        stripe.default_http_client = orig_attrs["default_http_client"]
        stripe.max_network_retries = orig_attrs["max_network_retries"]
        stripe.proxy = orig_attrs["proxy"]

    def setup_mock_server(self, handler):
        # Configure mock server.
        # Passing 0 as the port will cause a random free port to be chosen.
        self.mock_server = HTTPServer(("localhost", 0), handler)
        _, self.mock_server_port = self.mock_server.server_address

        # Start running mock server in a separate thread.
        # Daemon threads automatically shut down when the main process exits.
        self.mock_server_thread = Thread(target=self.mock_server.serve_forever)
        self.mock_server_thread.setDaemon(True)
        self.mock_server_thread.start()

    def test_hits_api_base(self):
        class MockServerRequestHandler(BaseHTTPRequestHandler):
            num_requests = 0

            def do_GET(self):
                self.__class__.num_requests += 1

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.end_headers()
                self.wfile.write(json.dumps({}).encode("utf-8"))
                return

        self.setup_mock_server(MockServerRequestHandler)

        stripe.api_base = "http://localhost:%s" % self.mock_server_port
        stripe.Balance.retrieve()
        assert MockServerRequestHandler.num_requests == 1

    def test_hits_proxy_through_default_http_client(self):
        class MockServerRequestHandler(BaseHTTPRequestHandler):
            num_requests = 0

            def do_GET(self):
                self.__class__.num_requests += 1

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.end_headers()
                self.wfile.write(json.dumps({}).encode("utf-8"))
                return

        self.setup_mock_server(MockServerRequestHandler)

        stripe.proxy = "http://localhost:%s" % self.mock_server_port
        stripe.Balance.retrieve()
        assert MockServerRequestHandler.num_requests == 1

        stripe.proxy = "http://bad-url"

        with warnings.catch_warnings(record=True) as w:
            stripe.Balance.retrieve()
            assert len(w) == 1
            assert "stripe.proxy was updated after sending a request" in str(
                w[0].message
            )

        assert MockServerRequestHandler.num_requests == 2

    def test_hits_proxy_through_custom_client(self):
        class MockServerRequestHandler(BaseHTTPRequestHandler):
            num_requests = 0

            def do_GET(self):
                self.__class__.num_requests += 1

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.end_headers()
                self.wfile.write(json.dumps({}).encode("utf-8"))
                return

        self.setup_mock_server(MockServerRequestHandler)

        stripe.default_http_client = stripe.http_client.new_default_http_client(
            proxy="http://localhost:%s" % self.mock_server_port
        )
        stripe.Balance.retrieve()
        assert MockServerRequestHandler.num_requests == 1

    def _test_client_is_thread_safe(self, client_ctor):
        class MockServerRequestHandler(BaseHTTPRequestHandler):
            num_requests = 0
            lock = Lock()

            def do_GET(self):
                with self.__class__.lock:
                    self.__class__.num_requests += 1
                    req_num = self.__class__.num_requests

                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8"
                )
                self.end_headers()
                self.wfile.write(
                    json.dumps({"req_num": req_num}).encode("utf-8")
                )
                return

        self.setup_mock_server(MockServerRequestHandler)
        stripe.api_base = "http://localhost:%s" % self.mock_server_port

        stripe.default_http_client = client_ctor()

        seen_responses = set()
        seen_responses_lock = Lock()

        def work():
            res = stripe.Balance.retrieve()
            req_num = res["req_num"]
            with seen_responses_lock:
                seen_responses.add(req_num)

        threads = [Thread(target=work) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # server should have seen 10 unique requests
        assert MockServerRequestHandler.num_requests == 10
        # client should have seen 10 unique responses
        assert len(seen_responses) == 10

    def test_requests_client_thread_safety(self):
        self._test_client_is_thread_safe(stripe.http_client.RequestsClient)

    @pytest.mark.skipif(not stripe.http_client.urlfetch, reason="requires urlfetch")
    def test_urlfetch_client_thread_safety(self):
        self._test_client_is_thread_safe(stripe.http_client.UrlFetchClient)

    @pytest.mark.skipif(not stripe.http_client.pycurl, reason="requires pycurl")
    def test_pycurl_client_thread_safety(self):
        self._test_client_is_thread_safe(stripe.http_client.PycurlClient)

    def test_urllib2_client_thread_safety(self):
        self._test_client_is_thread_safe(stripe.http_client.Urllib2Client)

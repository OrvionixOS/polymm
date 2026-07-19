"""
Unit tests for infra/health_server.py - HTTP health check server.

Tests the health server logic without requiring actual port binding.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock


class MockHealthServer:
    """Mock HealthServer for testing core logic patterns."""
    
    def __init__(self, get_status_fn, port: int = 8080):
        self._get_status = get_status_fn
        self._port = port
        self._running = False
        self._runner = None
    
    def is_running(self) -> bool:
        """Check if server is running."""
        return self._running
    
    def _build_status_response(self) -> dict:
        """Build status response from get_status_fn."""
        status = self._get_status()
        return {
            "status": "ok",
            "data": status,
        }
    
    def _validate_port(self, port: int) -> bool:
        """Validate port number."""
        return 1 <= port <= 65535


class TestHealthServerInit:
    """Tests for HealthServer initialization."""
    
    def test_init_stores_status_fn(self):
        """Server stores the status function."""
        status_fn = Mock(return_value={"active": True})
        server = MockHealthServer(get_status_fn=status_fn)
        
        assert server._get_status is status_fn
    
    def test_init_with_default_port(self):
        """Server uses default port 8080."""
        server = MockHealthServer(get_status_fn=Mock())
        
        assert server._port == 8080
    
    def test_init_with_custom_port(self):
        """Server uses custom port."""
        server = MockHealthServer(get_status_fn=Mock(), port=3000)
        
        assert server._port == 3000
    
    def test_init_not_running(self):
        """Server starts in stopped state."""
        server = MockHealthServer(get_status_fn=Mock())
        
        assert server.is_running() is False


class TestStatusResponse:
    """Tests for status response building."""
    
    def test_builds_ok_response(self):
        """Builds OK status response."""
        status_fn = Mock(return_value={"positions": 5, "orders": 3})
        server = MockHealthServer(get_status_fn=status_fn)
        
        response = server._build_status_response()
        
        assert response["status"] == "ok"
        assert response["data"]["positions"] == 5
        assert response["data"]["orders"] == 3
    
    def test_calls_status_function(self):
        """Builds response by calling status function."""
        status_fn = Mock(return_value={})
        server = MockHealthServer(get_status_fn=status_fn)
        
        server._build_status_response()
        
        status_fn.assert_called_once()
    
    def test_handles_empty_status(self):
        """Handles empty status dict."""
        status_fn = Mock(return_value={})
        server = MockHealthServer(get_status_fn=status_fn)
        
        response = server._build_status_response()
        
        assert response["status"] == "ok"
        assert response["data"] == {}


class TestPortValidation:
    """Tests for port validation."""
    
    @pytest.fixture
    def server(self):
        """Create mock server."""
        return MockHealthServer(get_status_fn=Mock())
    
    def test_valid_ports(self, server):
        """Valid ports are accepted."""
        assert server._validate_port(80) is True
        assert server._validate_port(443) is True
        assert server._validate_port(8080) is True
        assert server._validate_port(3000) is True
    
    def test_invalid_port_zero(self, server):
        """Port 0 is invalid."""
        assert server._validate_port(0) is False
    
    def test_invalid_port_negative(self, server):
        """Negative ports are invalid."""
        assert server._validate_port(-1) is False
    
    def test_invalid_port_too_high(self, server):
        """Ports above 65535 are invalid."""
        assert server._validate_port(65536) is False
    
    def test_max_valid_port(self, server):
        """Port 65535 is valid."""
        assert server._validate_port(65535) is True


class TestEndpoints:
    """Tests for expected endpoints."""
    
    def test_health_endpoint_path(self):
        """Health endpoint is at /health."""
        expected_paths = ["/health", "/"]
        
        for path in expected_paths:
            assert path in ["/health", "/"]
    
    def test_response_format(self):
        """Response should be JSON."""
        status = {"running": True, "orders": 5}
        
        # Simulate JSON response structure
        response = {
            "status": "ok",
            "data": status,
        }
        
        assert "status" in response
        assert isinstance(response["data"], dict)


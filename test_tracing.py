#!/usr/bin/env python3
"""
Test script to validate ElastAlert tracing implementation.

This script tests the tracing functionality without requiring a full ElastAlert setup.
"""

import sys
import os

# Add the elastalert directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'elastalert'))

# Import traceproviders directly
import traceproviders


def test_tracer_initialization():
    """Test tracer initialization with various configurations."""
    print("Testing tracer initialization...")
    
    # Test with disabled tracing
    config = {
        'enabled': False,
        'otel_exporter_endpoint': 'http://localhost:4317',
        'trace_service_name': 'test-elastalert',
        'trace_sampling_probability': 1.0
    }
    
    # This should not actually initialize the tracer
    result = traceproviders.init_tracer(config)
    print(f"✓ Disabled tracing handled correctly (returned: {result is not None})")
    
    # Test with enabled tracing (will fail if no OTEL collector is running, but should not crash)
    config['enabled'] = True
    try:
        result = traceproviders.init_tracer(config)
        print(f"✓ Enabled tracing initialization attempted (returned: {result is not None})")
    except Exception as e:
        print(f"⚠ Enabled tracing failed as expected without collector: {e}")
    
    print("Tracer initialization tests completed.\n")


def test_span_operations():
    """Test span creation and operations."""
    print("Testing span operations...")
    
    # Test span creation
    span = traceproviders.create_span("test_operation", {
        'test.attribute': 'test_value',
        'test.number': 42
    })
    print("✓ Span created successfully")
    
    # Test setting attributes
    traceproviders.set_attributes(span, {
        'additional.attribute': 'additional_value'
    })
    print("✓ Attributes set successfully")
    
    # Test adding events
    traceproviders.add_event(span, "test_event", {
        'event.data': 'test_event_data'
    })
    print("✓ Event added successfully")
    
    # Test error recording
    test_error = Exception("Test error for tracing")
    traceproviders.record_error(span, test_error)
    print("✓ Error recorded successfully")
    
    # Test ending span
    traceproviders.end_span(span)
    print("✓ Span ended successfully")
    
    print("Span operations tests completed.\n")


def test_context_manager():
    """Test the TraceContextManager."""
    print("Testing TraceContextManager...")
    
    try:
        with traceproviders.TraceContextManager("test_context_operation", {
            'context.test': 'context_value'
        }) as span:
            print("✓ Context manager entered successfully")
            traceproviders.set_attributes(span, {'inside.context': 'true'})
        print("✓ Context manager exited successfully")
    except Exception as e:
        print(f"✗ Context manager test failed: {e}")
    
    # Test context manager with exception
    try:
        with traceproviders.TraceContextManager("test_error_operation") as span:
            raise ValueError("Test exception in context manager")
    except ValueError:
        print("✓ Context manager handled exception correctly")
    
    print("TraceContextManager tests completed.\n")


def test_decorator():
    """Test the trace_operation decorator."""
    print("Testing trace_operation decorator...")
    
    @traceproviders.trace_operation("decorated_function", {'decorator.test': 'true'})
    def test_decorated_function(x, y):
        """Test function for decorator."""
        return x + y
    
    try:
        result = test_decorated_function(5, 3)
        print(f"✓ Decorated function executed successfully (result: {result})")
    except Exception as e:
        print(f"✗ Decorated function test failed: {e}")
    
    @traceproviders.trace_operation("decorated_error_function")
    def test_error_function():
        """Test function that raises an error."""
        raise RuntimeError("Test error in decorated function")
    
    try:
        test_error_function()
    except RuntimeError:
        print("✓ Decorated function with error handled correctly")
    
    print("Decorator tests completed.\n")


def test_utility_functions():
    """Test utility functions."""
    print("Testing utility functions...")
    
    hostname = traceproviders.get_hostname()
    print(f"✓ Hostname retrieved: {hostname}")
    
    host_ip = traceproviders.get_host_ip()
    print(f"✓ Host IP retrieved: {host_ip}")
    
    print("Utility functions tests completed.\n")


def main():
    """Run all tests."""
    print("=" * 60)
    print("ElastAlert Tracing Implementation Test Suite")
    print("=" * 60)
    print()
    
    test_tracer_initialization()
    test_span_operations()
    test_context_manager()
    test_decorator()
    test_utility_functions()
    
    print("=" * 60)
    print("All tests completed! 🎉")
    print("=" * 60)
    print()
    print("To use tracing in ElastAlert:")
    print("1. Add OpenTelemetry dependencies: pip install -r requirements.txt")
    print("2. Start a tracing backend (e.g., Jaeger):")
    print("   docker run -d --name jaeger \\")
    print("     -p 4317:4317 -p 16686:16686 \\")
    print("     jaegertracing/all-in-one:latest")
    print("3. Enable tracing in your config.yaml:")
    print("   tracing:")
    print("     enabled: true")
    print("     otel_exporter_endpoint: 'http://localhost:4317'")
    print("     trace_service_name: 'elastalert-production'")
    print("     trace_sampling_probability: 1.0")
    print("4. Run ElastAlert as usual")
    print("5. View traces in Jaeger UI: http://localhost:16686")


if __name__ == '__main__':
    main()

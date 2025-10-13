# -*- coding: utf-8 -*-
"""
OpenTelemetry tracing providers for ElastAlert.

This module provides a unified interface for distributed tracing in ElastAlert,
similar to the traceproviders package in haystack-router.
"""

import logging
import socket
from typing import Optional, Dict, Any, Callable

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, Span
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status, StatusCode

# Global tracer and provider instances
tracer: Optional[trace.Tracer] = None
trace_provider: Optional[TracerProvider] = None
telemetry_sdk_name = "opentelemetry"


def init_tracer(config: Dict[str, Any]) -> Optional[Callable]:
    """
    Initialize the OpenTelemetry tracer with the given configuration.
    
    Args:
        config: Dictionary containing tracing configuration with keys:
            - otel_exporter_endpoint: OTLP gRPC endpoint (e.g., "localhost:4317")
            - otel_sdk_version: OpenTelemetry SDK version
            - trace_service_name: Service name for traces
            - trace_sampling_probability: Sampling probability (0.0-1.0)
    
    Returns:
        Shutdown function for graceful cleanup, or None if initialization failed
    """
    global tracer, trace_provider
    
    if trace_provider is not None:
        return trace_provider.shutdown
    
    try:
        # Create resource with service information
        resource = Resource.create({
            ResourceAttributes.SERVICE_NAME: config.get('trace_service_name', 'elastalert'),
            ResourceAttributes.TELEMETRY_SDK_NAME: telemetry_sdk_name,
            ResourceAttributes.TELEMETRY_SDK_LANGUAGE: "python",
            ResourceAttributes.TELEMETRY_SDK_VERSION: config.get('otel_sdk_version', '1.25.0'),
            ResourceAttributes.HOST_NAME: get_hostname(),
            ResourceAttributes.HOST_ID: get_host_ip(),  # Using HOST_ID for IP address
        })
        
        # Create OTLP exporter
        otlp_exporter = OTLPSpanExporter(
            endpoint=config.get('otel_exporter_endpoint', 'http://localhost:4317'),
            insecure=True  # Note: Use secure connections in production
        )
        
        # Create tracer provider with sampling
        trace_provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(
                root=TraceIdRatioBased(
                    config.get('trace_sampling_probability', 1.0)
                )
            )
        )
        
        # Add span processor
        span_processor = BatchSpanProcessor(otlp_exporter)
        trace_provider.add_span_processor(span_processor)
        
        # Set global tracer provider
        trace.set_tracer_provider(trace_provider)
        
        # Create tracer instance
        tracer = trace.get_tracer("elastalert-service")
        
        logging.getLogger('elastalert').info("OpenTelemetry tracing initialized successfully")
        
        return trace_provider.shutdown
        
    except Exception as e:
        logging.getLogger('elastalert').error(f"Failed to initialize tracing: {e}")
        return None


def create_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> trace.Span:
    """
    Create a new span with the given name and attributes.
    
    Args:
        name: Name of the span
        attributes: Optional dictionary of span attributes
    
    Returns:
        OpenTelemetry Span object
    """
    if tracer is None:
        # Return a no-op span if tracer is not initialized
        return trace.NonRecordingSpan(trace.SpanContext(
            trace_id=0,
            span_id=0,
            is_remote=False
        ))
    
    span = tracer.start_span(name)
    
    if attributes:
        for key, value in attributes.items():
            span.set_attribute(key, value)
    
    return span


def end_span(span: trace.Span):
    """
    End the given span.
    
    Args:
        span: Span to end
    """
    if span:
        span.end()


def record_error(span: trace.Span, error: Exception):
    """
    Record an error on the given span.
    
    Args:
        span: Span to record error on
        error: Exception to record
    """
    if span and error:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))


def add_event(span: trace.Span, name: str, attributes: Optional[Dict[str, Any]] = None):
    """
    Add an event to the given span.
    
    Args:
        span: Span to add event to
        name: Name of the event
        attributes: Optional event attributes
    """
    if span:
        span.add_event(name, attributes or {})


def set_attributes(span: trace.Span, attributes: Dict[str, Any]):
    """
    Set attributes on the given span.
    
    Args:
        span: Span to set attributes on
        attributes: Dictionary of attributes to set
    """
    if span and attributes:
        for key, value in attributes.items():
            span.set_attribute(key, value)


def get_hostname() -> str:
    """
    Get the hostname of the current machine.
    
    Returns:
        Hostname string
    """
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def get_host_ip() -> str:
    """
    Get the IP address of the current machine.
    
    Returns:
        IP address string
    """
    try:
        # Connect to a dummy address to find the local IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class TraceContextManager:
    """
    Context manager for creating and managing spans with automatic cleanup.
    """
    
    def __init__(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        self.name = name
        self.attributes = attributes or {}
        self.span: Optional[trace.Span] = None
    
    def __enter__(self) -> trace.Span:
        self.span = create_span(self.name, self.attributes)
        return self.span
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            record_error(self.span, exc_val)
        end_span(self.span)


def trace_operation(name: str, attributes: Optional[Dict[str, Any]] = None):
    """
    Decorator for tracing function operations.
    
    Args:
        name: Name of the span
        attributes: Optional span attributes
    
    Returns:
        Decorated function
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            with TraceContextManager(name, attributes) as span:
                set_attributes(span, {
                    'function.name': func.__name__,
                    'function.module': func.__module__,
                })
                return func(*args, **kwargs)
        return wrapper
    return decorator
